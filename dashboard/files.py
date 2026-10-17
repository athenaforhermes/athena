"""Athena file browser — the app-folder explorer, mounted at /api/plugins/athena/files/.

Everything here is scoped to Athena's home: paths outside it are refused, so an
installed app's folder is browsable and the rest of the disk is not. That is the
difference from the standalone file browser this was ported from, which was
deliberately whole-disk.

Kept whole because a local AI workspace needs it whole: previews for images,
video, audio and generated 3D models (STL/OBJ), thumbnails, search/grep, zip and
extract, and delete-to-recycle-bin (send2trash) with the UI confirming first.
Long operations (copy/move/zip/extract) are jobs, never blocking requests.
"""
from __future__ import annotations

import base64
import codecs
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request as FastAPIRequest
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

# plugins/athena — the sandbox below resolves Athena's home relative to this.
PLUGIN_DIR = Path(__file__).resolve().parent.parent

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".ts"}
MODEL_EXTS = {".stl", ".obj"}
MAX_LIST = 8000
READ_CAP_KB = 256
THUMB_SRC_CAP_MB = 25


class PathIn(BaseModel):
    path: str = ""
    show_hidden: bool = False


class ReadIn(BaseModel):
    path: str = ""
    max_kb: int = READ_CAP_KB


class ThumbIn(BaseModel):
    path: str = ""
    size: int = 256


class MediaIn(BaseModel):
    path: str = ""
    size: int = 256


THUMB_CACHE_DIR = Path(tempfile.gettempdir()) / "athena-thumbs"
THUMB_CACHE_MAX_FILES = 2000
THUMB_CACHE_MAX_BYTES = 500 * 1024 * 1024


def _thumb_key(path: str, kind: str, size: int) -> Optional[str]:
    """Cache key binding path + content version (mtime, size)."""
    try:
        st = os.stat(path)
        sig = f"{path}|{st.st_mtime_ns}|{st.st_size}|{kind}|{size}"
        return hashlib.sha1(sig.encode("utf-8")).hexdigest() + ".jpg"
    except Exception:
        return None


def _thumb_get(key: Optional[str]) -> Optional[bytes]:
    if not key:
        return None
    try:
        p = THUMB_CACHE_DIR / key
        if p.is_file():
            return p.read_bytes()
    except Exception:
        pass
    return None


def _thumb_put(key: Optional[str], data: bytes) -> None:
    if not key or not data:
        return
    try:
        THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (THUMB_CACHE_DIR / key).write_bytes(data)
    except Exception:
        return
    # Opportunistic prune: oldest first, only when over a limit.
    try:
        files = [(f.stat().st_mtime, f.stat().st_size, f)
                 for f in THUMB_CACHE_DIR.iterdir() if f.is_file()]
        total = sum(s for _, s, _ in files)
        if len(files) > THUMB_CACHE_MAX_FILES or total > THUMB_CACHE_MAX_BYTES:
            files.sort()
            kept = len(files)
            for _, s, f in files:
                if kept <= THUMB_CACHE_MAX_FILES // 2 and total <= THUMB_CACHE_MAX_BYTES // 2:
                    break
                try:
                    f.unlink()
                    total -= s
                    kept -= 1
                except Exception:
                    pass
    except Exception:
        pass


@router.post("/videothumb")
def videothumb(body: MediaIn) -> JSONResponse:
    if Path(body.path).suffix.lower() not in VIDEO_EXTS:
        return JSONResponse({"error": "not a video"}, status_code=400)
    if (d := _deny_secret(Path(body.path))):
        return d
    ff = shutil.which("ffmpeg")
    if not ff:
        return JSONResponse({"error": "ffmpeg missing"}, status_code=501)
    size = max(32, min(body.size, 512))
    key = _thumb_key(body.path, "video", size)
    hit = _thumb_get(key)
    if hit is not None:
        return JSONResponse(
            {"data_url": f"data:image/jpeg;base64,{base64.b64encode(hit).decode('ascii')}",
             "cached": True})
    t = 1.0
    fp = shutil.which("ffprobe")
    if fp:
        try:
            out = subprocess.run(
                [fp, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", body.path],
                capture_output=True, text=True, timeout=15)
            dur = float((out.stdout or "").strip())
            t = min(5.0, max(0.5, dur * 0.1))
        except Exception:
            pass
    try:
        out = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-ss", str(t),
             "-i", body.path, "-frames:v", "1", "-vf", f"scale={size}:-1",
             "-q:v", "5", "-f", "mjpeg", "-"],
            capture_output=True, timeout=30)
        if out.returncode != 0 or not out.stdout:
            return JSONResponse({"error": "no frame"}, status_code=422)
        _thumb_put(key, bytes(out.stdout))
        b64 = base64.b64encode(out.stdout).decode("ascii")
        return JSONResponse({"data_url": f"data:image/jpeg;base64,{b64}"})
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class RawIn(BaseModel):
    path: str = ""
    max_mb: int = 48


@router.post("/raw")
def raw_bytes(body: RawIn) -> JSONResponse:
    enforce(body.path)
    if (d := _deny_secret(Path(body.path))):
        return d
    try:
        cap = max(1, min(body.max_mb, 96)) * 1024 * 1024
        with open(body.path, "rb") as f:
            raw = f.read(cap + 1)
        truncated = len(raw) > cap
        raw = raw[:cap]
        return JSONResponse({
            "b64": base64.b64encode(raw).decode("ascii"),
            "size": len(raw), "truncated": truncated})
    except FileNotFoundError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


def _stl_facets(path: str, cap: int = 4000):
    """Return (m,3,3) float array of triangles, sampled down to cap. Needs numpy."""
    import numpy as np
    with open(path, "rb") as f:
        buf = f.read()
    tris = None
    if len(buf) > 84:
        import struct
        (count,) = struct.unpack("<I", buf[80:84])
        if 0 < count < 5000000 and 84 + count * 50 == len(buf):
            dt = np.dtype([("n", "<f4", 3), ("v", "<f4", 9), ("a", "<u2")])
            arr = np.frombuffer(buf[84:84 + count * 50], dtype=dt)
            tris = arr["v"].reshape(-1, 3, 3).astype(np.float64)
    if tris is None:
        text = buf[:32 * 1024 * 1024].decode("utf-8", errors="ignore")
        verts = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("vertex"):
                try:
                    verts.append([float(x) for x in s.split()[1:4]])
                except ValueError:
                    pass
                if len(verts) >= cap * 3:
                    break
        if len(verts) < 3:
            return None
        tris = np.array(verts[:len(verts) // 3 * 3], dtype=np.float64).reshape(-1, 3, 3)
    if len(tris) > cap:
        tris = tris[:: (len(tris) + cap - 1) // cap]
    return tris


def _obj_facets(path: str, cap: int = 4000):
    import numpy as np
    verts: List[List[float]] = []
    faces: List[List[int]] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(f):
            if i > 2000000:
                break
            if line.startswith("v "):
                try:
                    verts.append([float(x) for x in line.split()[1:4]])
                except ValueError:
                    pass
            elif line.startswith("f "):
                try:
                    idx = [int(p.split("/")[0]) - 1 for p in line.split()[1:]]
                    if len(idx) >= 3:
                        for k in range(1, len(idx) - 1):
                            faces.append([idx[0], idx[k], idx[k + 1]])
                except ValueError:
                    pass
                if len(faces) >= cap:
                    break
    if not faces or not verts:
        return None
    v = np.array(verts, dtype=np.float64)
    tris = []
    for a, b, c in faces:
        if 0 <= a < len(v) and 0 <= b < len(v) and 0 <= c < len(v):
            tris.append([v[a], v[b], v[c]])
    return np.array(tris) if tris else None


def _render_facets(tris, size: int) -> bytes:
    from PIL import Image, ImageDraw
    import numpy as np
    c = tris.reshape(-1, 3)
    span = (c.max(0) - c.min(0)).max() or 1e-9
    t = (tris - c.min(0)) / span - 0.5
    az, el = 0.6, 0.45
    ca, sa, ce, se = np.cos(az), np.sin(az), np.cos(el), np.sin(el)
    x = t[..., 0] * ca + t[..., 2] * sa
    z = -t[..., 0] * sa + t[..., 2] * ca
    y = t[..., 1] * ce - z * se
    z2 = t[..., 1] * se + z * ce
    e1, e2 = x[:, 1] - x[:, 0], y[:, 1] - y[:, 0]
    f1, f2 = x[:, 2] - x[:, 0], y[:, 2] - y[:, 0]
    nz = e1 * f2 - e2 * f1
    nx = (y[:, 1] - y[:, 0]) * (z2[:, 2] - z2[:, 0]) - (z2[:, 1] - z2[:, 0]) * (y[:, 2] - y[:, 0])
    ny = (z2[:, 1] - z2[:, 0]) * (x[:, 2] - x[:, 0]) - (x[:, 1] - x[:, 0]) * (z2[:, 2] - z2[:, 0])
    ln = np.sqrt(nx * nx + ny * ny + nz * nz) + 1e-9
    light = np.array([0.4, 0.5, 0.75])
    light /= np.linalg.norm(light)
    s = np.clip((nx * light[0] + ny * light[1] + nz * light[2]) / ln, 0, 1)
    order = np.argsort(z2.mean(1))
    im = Image.new("RGB", (size, size), (18, 18, 24))
    d = ImageDraw.Draw(im)
    px = (x * 0.85 + 0.5) * size
    py = (0.5 - y * 0.85) * size
    for i in order:
        g = int(45 + 175 * s[i])
        d.polygon([float(px[i, 0]), float(py[i, 0]), float(px[i, 1]),
                   float(py[i, 1]), float(px[i, 2]), float(py[i, 2])],
                  fill=(g, g, min(255, g + 12)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    return buf.getvalue()


@router.post("/modelthumb")
def modelthumb(body: MediaIn) -> JSONResponse:
    ext = Path(body.path).suffix.lower()
    if ext not in MODEL_EXTS:
        return JSONResponse({"error": "not a model"}, status_code=400)
    size = max(32, min(body.size, 512))
    if (d := _deny_secret(Path(body.path))):
        return d
    try:
        key = _thumb_key(body.path, "model", size)
        hit = _thumb_get(key)
        if hit is not None:
            return JSONResponse(
                {"data_url": f"data:image/jpeg;base64,{base64.b64encode(hit).decode('ascii')}",
                 "cached": True})
        tris = _stl_facets(body.path) if ext == ".stl" else _obj_facets(body.path)
        if tris is None or not len(tris):
            return JSONResponse({"error": "no geometry"}, status_code=422)
        img = _render_facets(tris, size)
        _thumb_put(key, img)
        return JSONResponse(
            {"data_url": f"data:image/jpeg;base64,{base64.b64encode(img).decode('ascii')}",
             "tris": int(len(tris))})
    except FileNotFoundError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class MkdirIn(BaseModel):
    parent: str = ""
    name: str = ""


BAD_NAME_CHARS = '/\\:*?"<>|'


def _clean_name(name: str) -> str:
    return (name or "").strip().rstrip(". ")


def _free_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suffix = (p.stem, p.suffix) if p.suffix else (p.name, "")
    # for dirs p.suffix may be a fake extension (e.g. "data.old") — treat
    # dotted dir names carefully: only split when it's actually a file
    if p.is_dir():
        stem, suffix = p.name, ""
    i = 2
    while True:
        cand = p.parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


def _athena_home() -> Path:
    """Athena's home, resolved exactly as plugin_api.py resolves it.

    Read from settings.json so a relocated home stays one answer, not two.
    """
    h = PLUGIN_DIR.parent.parent / "athena"
    try:
        cfg = json.loads((PLUGIN_DIR / "settings.json").read_text(encoding="utf-8"))
        v = (cfg or {}).get("home")
        if v:
            h = Path(str(v).replace("\\", "/"))
    except Exception:
        pass
    try:
        return h.resolve()
    except Exception:
        return h


# Which folders this browser may look at, and which it may change. Two scopes, because reading
# and writing are not the same act.
#
# Reported: "nobody fixed the file browser tab. i can still only access athenas folders and the
# recycle bin on my computer". It was one predicate -- `_inside_home` -- and `/roots` had been
# advertising `drives` all along, with the panel already drawing them as shortcuts, so every click
# on C: or E: navigated and then answered 403. The drives were visible and unusable.
#
# Reading is wide on purpose: picking a file to hand an app, or pointing one at a folder, is what a
# person does in Explorer on their own machine, and the agent's tool calls run with the same rights
# the person has. Writing is narrow for the same reason in reverse: there is no confirmation step
# between a model's tool call and `os.remove`, and a person's own files live in their own folder.
# The line that keeps this from being a hole is `_deny_secret` below, not the roots.
_DRIVE_TTL_SECONDS = 5.0
_DRIVES_CACHE: Dict[str, Any] = {"at": 0.0, "list": []}


def _fixed_drives() -> List[Path]:
    """Each fixed disk, as a root.

    `all=False` is the point: a network share, a card reader or an optical drive is not a fixed
    disk, and enumerating those -- or blocking on a sleeping one -- must not happen inside a path
    check. Cached for a few seconds because this is asked once per path, and on Windows each entry
    costs a syscall.
    """
    import time
    now = time.monotonic()
    if _DRIVES_CACHE["list"] and now - float(_DRIVES_CACHE["at"]) < _DRIVE_TTL_SECONDS:
        return list(_DRIVES_CACHE["list"])
    out: List[Path] = []
    try:
        import psutil
        for part in psutil.disk_partitions(all=False):
            mp = str(getattr(part, "mountpoint", "") or "").strip()
            if not mp:
                continue
            try:
                out.append(Path(os.path.realpath(mp)))
            except Exception:
                continue
    except Exception:
        pass
    if out:
        _DRIVES_CACHE["at"] = now
        _DRIVES_CACHE["list"] = out
    return out


def _read_roots() -> List[Path]:
    """Where this browser may look: Athena's home, then every fixed disk."""
    return [_athena_home()] + _fixed_drives()


def _write_roots() -> List[Path]:
    """Where it may change something: Athena's home, and the person's own folder."""
    out = [_athena_home()]
    try:
        out.append(Path(os.path.realpath(Path.home())))
    except Exception:
        pass
    return out


def _in_roots(p: Path, roots: List[Path]) -> bool:
    """True when p resolves inside one of these roots (realpath, so links cannot escape)."""
    try:
        rp = Path(os.path.realpath(p))
    except Exception:
        return False
    for root in roots:
        try:
            if rp == root or root in rp.parents:
                return True
        except Exception:
            continue
    return False


# The refusal an in-process caller looks for. One phrase, in one place, so a route cannot invent
# its own wording and a caller cannot miss the refusal in a string that happens to read fine.
REFUSED = "outside the folders Athena may"


def enforce(paths: Any, write: bool = False) -> None:
    """Refuse caller-supplied paths this browser may not touch. Raises, never returns.

    `sandbox_dep` covers HTTP requests, and it is the floor: it applies *read* scope to every
    request, so a new route cannot forget it. A route that changes something calls this again with
    `write=True` — and so does the agent's own door, which runs no dependency at all: the
    in-process caller is exactly the hole this function was written for (`list_dir("C:/Windows")`
    from a model tool call once answered with a real listing).

    Raises rather than returns: a dependency's return value is injected as a parameter and does not
    short-circuit anything, so returning a 403 would look correct and enforce nothing.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    roots = _write_roots() if write else _read_roots()
    for raw in (paths or []):
        if not raw:
            continue
        p = Path(str(raw).replace("\\", "/"))
        if not _in_roots(p, roots):
            raise HTTPException(status_code=403, detail={
                "error": (REFUSED + (" change: " if write else " read: ")) + str(raw),
                "roots": [str(r) for r in roots],
                # Said in the refusal so a person reading it in the panel knows what to do about
                # it, and so an agent can choose a folder that would work instead of retrying.
                "why": ("Athena only changes files in its own home and in your own folder"
                        if write else
                        "Athena reads Athena's home and your fixed disks, and refuses credential stores"),
            })
        # The second question, at the same floor. The roots decide *where*, this decides *what*,
        # and a route that remembered one and forgot the other is exactly the shape of bug this
        # function exists to end: `~/.ssh` is inside the write scope (your own folder), so scope
        # alone would hand over a private key. Routes keep their own `_deny_secret` calls for the
        # wording they already show, but nothing depends on them any more.
        if (why := _secret_reason(p)):
            raise HTTPException(status_code=403, detail={
                "error": "refusing " + why + ": " + str(raw),
                "why": "that is a credential store, a private key or a saved session, and "
                       "this browser neither reads nor writes one wherever it sits",
            })


def _guard_root(p: Path, write: bool = False) -> Optional[JSONResponse]:
    """The sandbox, for one path.

    Filebox deliberately browsed the whole disk ("same trust as the terminal")
    and guarded only its *writes* — every read route relied on `_deny_secret`,
    which allowed everything but a short deny list. Re-scoped to an app browser
    that is backwards: the port answered `list` on C:/Windows with HTTP 200 and
    a real directory listing.

    The model here is "validate what the caller asked for", so this is applied
    centrally to every caller-supplied path by `sandbox_dep` below, and kept as
    a direct call on the write paths that already had it (defence in depth).
    Paths the handler computes itself — the thumbnail cache, a zip preview under
    %TEMP%, the recycle bin — are not caller-supplied and are not checked here.
    """
    roots = _write_roots() if write else _read_roots()
    if not _in_roots(p, roots):
        return JSONResponse(
            {"error": REFUSED + (" change" if write else " read"), "roots": [str(r) for r in roots]},
            status_code=403)
    if (why := _secret_reason(p)):
        return JSONResponse({"error": "refusing " + why}, status_code=403)
    return None


# Body keys that carry a caller-supplied path. Derived from the request models
# themselves, not guessed: `parent` (MkdirIn, MkfileIn) was missing at first and
# left both routes unvalidated. Re-check this list whenever a model gains a
# field — a path under an unlisted key is a path the sandbox never sees.
#
# Deliberately excluded: `new_name` (a fragment joined onto a validated parent),
# `exts`/`pattern` (filters), `member` (a zip member checked separately), and
# every numeric/flag field.
_PATH_KEYS = ("path", "paths", "parent", "dest", "dest_dir", "root", "target", "src")


async def sandbox_dep(request: FastAPIRequest) -> None:
    """Refuse any request whose caller-supplied path this browser may not read.

    Read scope only: this is the floor under every route, and a route that changes something
    enforces write scope itself (files in the home and the person's own folder). Applied
    router-wide (see plugin_api.py) instead of per-route: the point is that a *new* read route
    cannot forget the check, which is exactly how the port left the disk readable.

    Raises rather than returns: a dependency's return value is injected as a
    parameter, it does not short-circuit the request. Returning a 403 here would
    look correct and enforce nothing.
    """
    found: List[str] = []

    def collect(key: str, value: Any) -> None:
        if key not in _PATH_KEYS:
            return
        if isinstance(value, str):
            if value.strip():
                found.append(value)
        elif isinstance(value, (list, tuple)):
            found.extend(v for v in value if isinstance(v, str) and v.strip())

    for key, value in request.query_params.items():
        collect(key, value)
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        for key, value in body.items():
            collect(key, value)
    elif isinstance(body, list):
        for item in body:
            if isinstance(item, dict):
                for key, value in item.items():
                    collect(key, value)

    roots = _read_roots()
    for raw in found:
        if not _in_roots(Path(raw), roots):
            raise HTTPException(
                status_code=403,
                detail={"error": REFUSED + " read: " + raw,
                        "roots": [str(r) for r in roots]},
            )
        # Both questions, at the floor: the roots decide *where*, this decides *what*, and the
        # list has to live somewhere a new route cannot forget it -- the same argument that put
        # this dependency here at all. The routes still call `_deny_secret` themselves so the
        # panel keeps their wording, but nothing depends on that any more.
        if (why := _secret_reason(Path(raw))):
            raise HTTPException(
                status_code=403,
                detail={"error": "refusing " + why + ": " + raw,
                        "why": "a credential store, a private key or a saved session is not "
                               "something this browser hands over, wherever it sits"},
            )



# What the browser will not hand over, now that it can see the whole disk. This list, not the
# roots, is what keeps a drive-wide browser from being a credential dumper: reads reach every fixed
# disk, so anything whose *value* is a secret has to be named here.
#
# The threat it answers is concrete: an installed app drives this browser, and an agent's tool
# calls are a model reading files into its own context. `~/.ssh/id_rsa` and Chrome's
# `Local State` + `Login Data` are decrypted passwords and keys on any machine that has ever
# signed into a browser, and the Windows hives are every local account's hashes. A file browser a
# person drives is not the risk; a file browser a model drives, on a disk it can now enumerate, is.
_SECRET_DIRS = ("cache/HF_AUTH", "credentials", "connect")

# Relative to the person's own folder: credential stores, not documents.
_SECRET_HOME_DIRS = (
    ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".password-store", ".config/gcloud",
    "AppData/Roaming/Microsoft/Credentials", "AppData/Local/Microsoft/Credentials",
)

# By name, anywhere on the disk. Browser and OS stores are portable between machines and are the
# usual way a stored password leaves one.
_SECRET_NAMES = (
    "login data", "login data-journal", "cookies", "cookies-journal", "logins.json",
    "key3.db", "key4.db", "local state", "signons.sqlite", "credential store",
    ".netrc", "_netrc", ".git-credentials", ".localadmintoken", "ntuser.dat",
    "id_rsa", "id_ed25519",
)

# The hives that hold every local account's hashes, relative to the system drive.
_SECRET_SYSTEM_DIRS = ("Windows/System32/config",)

# Inside Hermes' own folder. Not the whole tree: this plugin's source lives under it, and a person
# browsing their own machine should be able to look at it. These are the parts that are somebody's
# private business rather than a file -- saved conversations, the assistant's memory, and the
# config that holds API keys.
_SECRET_HERMES_DIRS = ("sessions", "memories")
_SECRET_HERMES_NAMES = ("config.yaml", "settings.json", "auth.json", ".env")


def _secret_reason(p: Path) -> str:
    """Why this path must not be handed over, in words, or "" when it may be.

    The *decision* is separate from the answer on purpose: a route answers with a body (better
    wording, a body the panel already knows how to draw) while the router-wide floor has to raise.
    Both ask this one question instead of one of them keeping a private copy of the list.

    It used to be narrower -- just our own HF token -- because the sandbox was one folder. Reads
    now reach every fixed disk, so anything whose *value* is a secret has to be named here: this
    list, not the roots, is what keeps a drive-wide browser from being a credential dumper. The
    threat is concrete rather than theoretical -- an installed app drives this browser, and an
    agent's tool calls are a model pulling files into its own context. `~/.ssh/id_rsa`, Chrome's
    `Local State` beside its `Login Data`, and the Windows hives are working keys and password
    hashes on any machine that has ever signed into a browser.

    Note what is still deliberately NOT inherited from filebox: its "any file named .env /
    config.yaml / token under a Hermes directory" rule. An installed AI app legitimately owns a
    .env and a settings.json, and Athena's home *is* inside the Hermes directory, so that rule
    would refuse the app's own config -- the one thing this browser exists to show. The Hermes-side
    names below are therefore checked outside the home, and `_SECRET_HERMES_DIRS` names the parts
    that are somebody's private business rather than a file: saved conversations and the
    assistant's memory.
    """
    try:
        rp = Path(os.path.realpath(p))
    except Exception:
        return "bad path"

    def under(root: Path) -> bool:
        try:
            r = Path(os.path.realpath(root))
        except Exception:
            return False
        return rp == r or r in rp.parents

    home = _athena_home()
    in_home = under(home)

    # Athena's own token directory first: it sits inside the home, which is otherwise allowed.
    for rel in _SECRET_DIRS:
        if under(home / rel):
            return "credential store"

    try:
        me = Path.home()
    except Exception:
        me = None
    if me is not None:
        if under(me / ".ssh"):
            return "~/.ssh"
        for rel in _SECRET_HOME_DIRS:
            if under(me / rel):
                return "credential store"

    if not in_home:
        hermes_root = PLUGIN_DIR.parent.parent
        for rel in _SECRET_HERMES_DIRS:
            if under(hermes_root / rel):
                return "private Hermes data"
        if hermes_root in rp.parents or rp == hermes_root:
            if rp.name.lower() in _SECRET_HERMES_NAMES:
                return "a file that holds credentials"

    drive = os.environ.get("SystemDrive") or "C:"
    for rel in _SECRET_SYSTEM_DIRS:
        if under(Path(drive + "/" + rel)):
            return "the Windows security hives"

    # Last, because it is the cheapest test and the broadest: a name that means a stored secret.
    if rp.name.lower() in _SECRET_NAMES:
        return "a credential store"
    return ""


def _deny_secret(p: Path) -> Optional[JSONResponse]:
    """The 403 a route answers with, or None. The same question as `_secret_reason`.

    Called as `if (d := _deny_secret(Path(x))): return d` in the routes -- what it refuses, it
    refuses in the panel's own words, for the reason the floor above also raises for.
    """
    why = _secret_reason(p)
    if not why:
        return None
    if why == "bad path":
        return JSONResponse({"error": "bad path"}, status_code=400)
    return JSONResponse({"error": "refusing " + why}, status_code=403)



class RenameIn(BaseModel):
    path: str = ""
    new_name: str = ""


@router.post("/rename")
def rename(body: RenameIn) -> JSONResponse:
    enforce(body.path, write=True)
    name = _clean_name(body.new_name)
    if not name or any(c in name for c in BAD_NAME_CHARS):
        return JSONResponse({"error": "bad name"}, status_code=400)
    try:
        src = Path(body.path)
        if not src.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        if (g := _guard_root(src)):
            return g
        if (d := _deny_secret(src)):
            return d
        dst = src.parent / name
        if dst.exists():
            return JSONResponse({"error": "already exists"}, status_code=409)
        src.rename(dst)
        return JSONResponse({"ok": True, "path": str(dst)})
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class OpIn(BaseModel):
    paths: List[str] = []
    dest_dir: str = ""


def _paste(op: str, body: OpIn) -> JSONResponse:
    # Write scope: a job that changes files enforces where it may change them, on top of the
    # read floor `sandbox_dep` applies to every request.
    enforce(body.paths)
    enforce(body.dest_dir, write=True)
    if not body.paths:
        return JSONResponse({"error": "nothing selected"}, status_code=400)
    try:
        dest = Path(body.dest_dir)
        if not dest.is_dir():
            return JSONResponse({"error": "bad destination"}, status_code=400)
        if (d := _deny_secret(dest)):
            return d
        done, failed = [], []
        for raw in body.paths:
            try:
                src = Path(raw)
                if not src.exists():
                    failed.append({"path": raw, "error": "not found"})
                    continue
                if (d := _deny_secret(src)):
                    failed.append({"path": raw, "error": "forbidden path"})
                    continue
                if op == "move" and (g := _guard_root(src)):
                    failed.append({"path": raw, "error": "drive root"})
                    continue
                if src.is_dir() and (dest.resolve() == src.resolve() or src.resolve() in dest.resolve().parents):
                    failed.append({"path": raw, "error": "can't paste into itself"})
                    continue
                dst = _free_path(dest / src.name)
                if op == "copy":
                    if src.is_dir():
                        # symlinks=True: copy links as links — never follow
                        # a linked tree off into the source or a loop.
                        shutil.copytree(src, dst, symlinks=True)
                    else:
                        shutil.copy2(src, dst, follow_symlinks=False)
                else:
                    shutil.move(str(src), str(dst))
                done.append(str(dst))
            except PermissionError:
                failed.append({"path": raw, "error": "access denied"})
            except Exception as e:
                failed.append({"path": raw, "error": str(e)[:120]})
        return JSONResponse({"ok": not failed, "done": done, "failed": failed})
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@router.post("/copy")
def copy(body: OpIn) -> JSONResponse:
    return _paste("copy", body)


@router.post("/move")
def move(body: OpIn) -> JSONResponse:
    return _paste("move", body)


class _Cancelled(Exception):
    """Raised inside a job worker when /jobcancel sets the flag."""


_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _fsize(p: str) -> int:
    try:
        return os.path.getsize(p)
    except Exception:
        return 0


def _job_new(op: str, label: str) -> str:
    jid = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid, "op": op, "label": label, "state": "running",
            "done_bytes": 0, "total_bytes": 0, "done_files": 0,
            "total_files": 0, "current": "", "failed": [],
            "result": None, "cancel": False,
        }
        stalled = [k for k, v in _jobs.items() if v["state"] != "running"]
        while len(_jobs) > 50 and stalled:
            _jobs.pop(stalled.pop(0), None)
    return jid


def _job_set(jid: str, **kw: Any) -> None:
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid].update(kw)


def _job_should_cancel(jid: str) -> bool:
    with _jobs_lock:
        return bool(_jobs.get(jid, {}).get("cancel"))


def _job_check(jid: str) -> None:
    if _job_should_cancel(jid):
        raise _Cancelled()


def _job_finish(jid: str, ok_result: Dict[str, Any]) -> None:
    cancelled = _job_should_cancel(jid)
    with _jobs_lock:
        job = _jobs.get(jid)
        if not job:
            return
        if cancelled:
            job["state"] = "cancelled"
            job["result"] = {"ok": False, "error": "cancelled",
                             "done": ok_result.get("done", []),
                             "failed": job["failed"]}
        else:
            job["state"] = "completed"
            job["result"] = ok_result


def _job_fail(jid: str, error: str, done: Any = None) -> None:
    _job_set(jid, state="failed",
             result={"ok": False, "error": error[:200],
                     "done": done or [], "failed": _jobs.get(jid, {}).get("failed", [])})


def _run_copy_job(jid: str, paths: List[str], dest_dir: str) -> None:
    done: List[str] = []
    failed: List[Dict[str, str]] = []
    try:
        dest = Path(dest_dir)
        valid: List[Path] = []
        for raw in paths:
            _job_check(jid)
            src = Path(raw)
            if not src.exists() and not src.is_symlink():
                failed.append({"path": raw, "error": "not found"})
                continue
            if _deny_secret(src):
                failed.append({"path": raw, "error": "forbidden path"})
                continue
            try:
                same = dest.resolve() == src.resolve() or src.resolve() in dest.resolve().parents
            except Exception:
                same = False
            if src.is_dir() and not src.is_symlink() and same:
                failed.append({"path": raw, "error": "can't paste into itself"})
                continue
            valid.append(src)
        # Plan: (src, dst, size, kind) — kind: file | dir | link. Links are
        # recreated as links, never followed.
        items: List[tuple] = []
        tops: List[tuple] = []
        for src in valid:
            top = _free_path(dest / src.name)
            tops.append((str(src), str(top)))
            if src.is_symlink():
                items.append((str(src), str(top), 0, "link"))
            elif src.is_file():
                items.append((str(src), str(top), _fsize(str(src)), "file"))
            elif src.is_dir():
                items.append((str(src), str(top), 0, "dir"))
                for root, dnames, fnames in os.walk(src, followlinks=False):
                    rp = Path(root)
                    rel = rp.relative_to(src)
                    for d in dnames:
                        full = rp / d
                        if full.is_symlink():
                            items.append((str(full), str(top / rel / d), 0, "link"))
                        else:
                            items.append((str(full), str(top / rel / d), 0, "dir"))
                    for fn in fnames:
                        full = rp / fn
                        if full.is_symlink():
                            items.append((str(full), str(top / rel / fn), 0, "link"))
                        else:
                            items.append((str(full), str(top / rel / fn), _fsize(str(full)), "file"))
        _job_set(jid, total_bytes=sum(s for _, _, s, k in items if k == "file"),
                 total_files=len(items))
        db = df = 0
        for src_s, dst_s, sz, kind in items:
            _job_check(jid)
            dp = Path(dst_s)
            try:
                if kind == "dir":
                    dp.mkdir(parents=True, exist_ok=True)
                elif kind == "link":
                    if not dp.exists() and not dp.is_symlink():
                        dp.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            os.symlink(os.readlink(src_s), dp)
                        except FileExistsError:
                            pass
                else:
                    dp.parent.mkdir(parents=True, exist_ok=True)
                    _job_set(jid, current=Path(src_s).name)
                    shutil.copy2(src_s, dp, follow_symlinks=False)
                    db += sz
            except Exception as e:
                failed.append({"path": src_s, "error": str(e)[:120]})
                with _jobs_lock:
                    if jid in _jobs:
                        _jobs[jid]["failed"].append({"path": src_s, "error": str(e)[:120]})
            df += 1
            _job_set(jid, done_bytes=db, done_files=df)
        done = [d for _, d in tops]
        _job_finish(jid, {"ok": not failed, "done": done, "failed": failed})
    except _Cancelled:
        _job_finish(jid, {"ok": not failed, "done": done, "failed": failed})
    except Exception as e:
        _job_fail(jid, str(e), done)


def _run_move_job(jid: str, paths: List[str], dest_dir: str) -> None:
    done: List[str] = []
    failed: List[Dict[str, str]] = []
    try:
        dest = Path(dest_dir)
        valid: List[Path] = []
        for raw in paths:
            _job_check(jid)
            src = Path(raw)
            if not src.exists() and not src.is_symlink():
                failed.append({"path": raw, "error": "not found"})
                continue
            if _deny_secret(src):
                failed.append({"path": raw, "error": "forbidden path"})
                continue
            if _guard_root(src):
                failed.append({"path": raw, "error": "drive root"})
                continue
            try:
                same = dest.resolve() == src.resolve() or src.resolve() in dest.resolve().parents
            except Exception:
                same = False
            if src.is_dir() and not src.is_symlink() and same:
                failed.append({"path": raw, "error": "can't paste into itself"})
                continue
            valid.append(src)
        _job_set(jid, total_bytes=0, total_files=len(valid))
        df = 0
        for src in valid:
            _job_check(jid)
            try:
                dst = _free_path(dest / src.name)
                _job_set(jid, current=src.name)
                shutil.move(str(src), str(dst))
                done.append(str(dst))
            except PermissionError:
                failed.append({"path": str(src), "error": "access denied"})
                with _jobs_lock:
                    if jid in _jobs:
                        _jobs[jid]["failed"].append({"path": str(src), "error": "access denied"})
            except Exception as e:
                failed.append({"path": str(src), "error": str(e)[:120]})
                with _jobs_lock:
                    if jid in _jobs:
                        _jobs[jid]["failed"].append({"path": str(src), "error": str(e)[:120]})
            df += 1
            _job_set(jid, done_files=df)
        _job_finish(jid, {"ok": not failed, "done": done, "failed": failed})
    except _Cancelled:
        _job_finish(jid, {"ok": not failed, "done": done, "failed": failed})
    except Exception as e:
        _job_fail(jid, str(e), done)


def _run_zip_job(jid: str, paths: List[str], name: str) -> None:
    failed: List[Dict[str, str]] = []
    zpath: Optional[Path] = None
    try:
        for raw in paths:
            _job_check(jid)
            if _deny_secret(Path(raw)):
                _job_fail(jid, "forbidden path")
                return
        first = Path(paths[0])
        parent = first.parent
        base = _clean_name(name) or (first.stem if len(paths) == 1 else "archive")
        if any(c in base for c in BAD_NAME_CHARS):
            _job_fail(jid, "bad name")
            return
        if not base.lower().endswith(".zip"):
            base += ".zip"
        zpath = _free_path(parent / base)
        items: List[tuple] = []  # (full | None, arcname)
        for raw in paths:
            src = Path(raw)
            if not src.exists():
                continue
            if src.is_dir() and not src.is_symlink():
                for root, dirs, files in os.walk(src):
                    rp = Path(root)
                    if not dirs and not files:
                        items.append((None, str(rp.relative_to(parent)) + "/"))
                    for fn in files:
                        fp = rp / fn
                        items.append((str(fp), str(fp.relative_to(parent))))
            else:
                items.append((str(src), src.name))
        _job_set(jid, total_bytes=sum(_fsize(f) for f, _ in items if f),
                 total_files=len(items))
        db = df = 0
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for full, arc in items:
                _job_check(jid)
                try:
                    _job_set(jid, current=Path(arc).name)
                    if full is None:
                        z.writestr(arc, "")
                    else:
                        z.write(full, arc)
                        db += _fsize(full)
                except Exception as e:
                    failed.append({"path": full or arc, "error": str(e)[:120]})
                    with _jobs_lock:
                        if jid in _jobs:
                            _jobs[jid]["failed"].append({"path": full or arc, "error": str(e)[:120]})
                df += 1
                _job_set(jid, done_bytes=db, done_files=df)
        _job_finish(jid, {"ok": not failed, "path": str(zpath), "failed": failed})
    except _Cancelled:
        try:
            if zpath and zpath.exists():
                os.remove(zpath)
        except Exception:
            pass
        _job_finish(jid, {"ok": False, "path": "", "failed": failed})
    except Exception as e:
        try:
            if zpath and zpath.exists():
                os.remove(zpath)
        except Exception:
            pass
        _job_fail(jid, str(e))


def _run_extract_job(jid: str, path: str, dest_dir: str) -> None:
    try:
        src = Path(path)
        dest = Path(dest_dir) if dest_dir else src.parent / src.stem
        if dest.exists() and not dest.is_dir():
            _job_fail(jid, "destination blocked")
            return
        dest = _free_path(dest) if dest.exists() else dest
        if _deny_secret(dest):
            _job_fail(jid, "forbidden path")
            return
        dest.mkdir(parents=True, exist_ok=True)
        dest_resolved = dest.resolve()
        with zipfile.ZipFile(src, "r") as z:
            members = z.infolist()
            for m in members:
                target = dest / m.filename
                try:
                    resolved = target.resolve()
                except Exception:
                    _job_fail(jid, "bad archive entry")
                    return
                if resolved != dest_resolved and dest_resolved not in resolved.parents:
                    _job_fail(jid, "archive has unsafe paths")
                    return
            _job_set(jid, total_bytes=sum(m.file_size for m in members),
                     total_files=len(members))
            db = df = 0
            for m in members:
                _job_check(jid)
                try:
                    _job_set(jid, current=Path(m.filename).name)
                    z.extract(m, dest)
                    db += m.file_size
                except Exception as e:
                    with _jobs_lock:
                        if jid in _jobs:
                            _jobs[jid]["failed"].append({"path": m.filename, "error": str(e)[:120]})
                df += 1
                _job_set(jid, done_bytes=db, done_files=df)
        _job_finish(jid, {"ok": True, "path": str(dest), "failed": []})
    except _Cancelled:
        _job_finish(jid, {"ok": False, "path": "", "failed": []})
    except Exception as e:
        _job_fail(jid, str(e))


class JobStartIn(BaseModel):
    op: str = ""
    paths: List[str] = []
    path: str = ""
    dest_dir: str = ""
    name: str = ""
    label: str = ""


@router.post("/jobstart")
def jobstart(body: JobStartIn) -> JSONResponse:
    """Start a background copy/move/zip/extract job. Returns {job_id} fast;
    poll /jobstatus. Sync /copy /move /zip /extract stay as-is."""
    # Write scope: a job that changes files enforces where it may change them, on top of the
    # read floor `sandbox_dep` applies to every request.
    enforce(body.paths)
    enforce(body.dest_dir, write=True)
    op = (body.op or "").lower()
    try:
        if op in ("copy", "move"):
            if not body.paths:
                return JSONResponse({"error": "nothing selected"}, status_code=400)
            dest = Path(body.dest_dir)
            if not dest.is_dir():
                return JSONResponse({"error": "bad destination"}, status_code=400)
            if (d := _deny_secret(dest)):
                return d
            label = body.label or f"{op} {len(body.paths)} item{'s' if len(body.paths) != 1 else ''}"
            jid = _job_new(op, label)
            worker = _run_copy_job if op == "copy" else _run_move_job
            threading.Thread(target=worker, args=(jid, list(body.paths), body.dest_dir),
                             daemon=True).start()
            return JSONResponse({"ok": True, "job_id": jid})
        if op == "zip":
            if not body.paths:
                return JSONResponse({"error": "nothing selected"}, status_code=400)
            for raw in body.paths:
                if (d := _deny_secret(Path(raw))):
                    return d
            label = body.label or f"zip {len(body.paths)} item{'s' if len(body.paths) != 1 else ''}"
            jid = _job_new(op, label)
            threading.Thread(target=_run_zip_job, args=(jid, list(body.paths), body.name),
                             daemon=True).start()
            return JSONResponse({"ok": True, "job_id": jid})
        if op == "extract":
            src = Path(body.path)
            if not src.is_file() or src.suffix.lower() != ".zip":
                return JSONResponse({"error": "not a zip"}, status_code=400)
            if (d := _deny_secret(src)):
                return d
            label = body.label or f"extract {src.name}"
            jid = _job_new(op, label)
            threading.Thread(target=_run_extract_job, args=(jid, body.path, body.dest_dir),
                             daemon=True).start()
            return JSONResponse({"ok": True, "job_id": jid})
        return JSONResponse({"error": "bad op"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class JobIdIn(BaseModel):
    job_id: str = ""


@router.post("/jobstatus")
def jobstatus(body: JobIdIn) -> JSONResponse:
    with _jobs_lock:
        job = _jobs.get(body.job_id)
        if not job:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        view = {k: v for k, v in job.items() if k != "cancel"}
    return JSONResponse(view)


@router.post("/jobcancel")
def jobcancel(body: JobIdIn) -> JSONResponse:
    with _jobs_lock:
        job = _jobs.get(body.job_id)
        if not job:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        if job["state"] != "running":
            return JSONResponse({"ok": True, "state": job["state"]})
        job["cancel"] = True
    return JSONResponse({"ok": True, "state": "cancelling"})


class DeleteIn(BaseModel):
    paths: List[str] = []


@router.post("/delete")
def delete(body: DeleteIn) -> JSONResponse:
    enforce(body.paths, write=True)
    if not body.paths:
        return JSONResponse({"error": "nothing selected"}, status_code=400)
    try:
        from send2trash import send2trash
    except ImportError:
        return JSONResponse(
            {"error": "send2trash missing — pip install send2trash (refusing permanent delete)"},
            status_code=501)
    done, failed = [], []
    for raw in body.paths:
        try:
            p = Path(raw)
            if not p.exists():
                failed.append({"path": raw, "error": "not found"})
                continue
            if (g := _guard_root(p)):
                failed.append({"path": raw, "error": "drive root"})
                continue
            if (d := _deny_secret(p)):
                failed.append({"path": raw, "error": "forbidden path"})
                continue
            send2trash(str(p))
            done.append(raw)
        except PermissionError:
            failed.append({"path": raw, "error": "access denied"})
        except Exception as e:
            failed.append({"path": raw, "error": str(e)[:120]})
    return JSONResponse({"ok": not failed, "done": done, "failed": failed})


class ZipIn(BaseModel):
    paths: List[str] = []
    name: str = ""


@router.post("/zip")
def make_zip(body: ZipIn) -> JSONResponse:
    enforce(body.paths)
    if not body.paths:
        return JSONResponse({"error": "nothing selected"}, status_code=400)
    try:
        first = Path(body.paths[0])
        parent = first.parent
        base = _clean_name(body.name) or (first.stem if len(body.paths) == 1 else "archive")
        if any(c in base for c in BAD_NAME_CHARS):
            return JSONResponse({"error": "bad name"}, status_code=400)
        for raw in body.paths:
            if (d := _deny_secret(Path(raw))):
                return d
        if not base.lower().endswith(".zip"):
            base += ".zip"
        zpath = _free_path(parent / base)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for raw in body.paths:
                src = Path(raw)
                if not src.exists():
                    continue
                if src.is_dir() and not src.is_symlink():
                    for root, dirs, files in os.walk(src):
                        rp = Path(root)
                        if not dirs and not files:
                            # zip has no implicit dir entries — keep empty dirs
                            z.writestr(str(rp.relative_to(parent)) + "/", "")
                        for fn in files:
                            fp = rp / fn
                            z.write(fp, fp.relative_to(parent))
                else:
                    z.write(src, src.name)
        return JSONResponse({"ok": True, "path": str(zpath)})
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class ExtractIn(BaseModel):
    path: str = ""
    dest_dir: str = ""


@router.post("/extract")
def extract_zip(body: ExtractIn) -> JSONResponse:
    # Write scope: a job that changes files enforces where it may change them, on top of the
    # read floor `sandbox_dep` applies to every request.
    enforce(body.path)
    enforce(body.dest_dir, write=True)
    try:
        src = Path(body.path)
        if not src.is_file() or src.suffix.lower() != ".zip":
            return JSONResponse({"error": "not a zip"}, status_code=400)
        if (d := _deny_secret(src)):
            return d
        dest = Path(body.dest_dir) if body.dest_dir else src.parent / src.stem
        if dest.exists() and not dest.is_dir():
            return JSONResponse({"error": "destination blocked"}, status_code=409)
        dest = _free_path(dest) if dest.exists() else dest
        if (d := _deny_secret(dest)):
            return d
        dest.mkdir(parents=True, exist_ok=True)
        dest_resolved = dest.resolve()
        with zipfile.ZipFile(src, "r") as z:
            for m in z.infolist():
                # zip-slip guard: every member must land inside dest
                target = dest / m.filename
                try:
                    resolved = target.resolve()
                except Exception:
                    return JSONResponse({"error": "bad archive entry"}, status_code=400)
                if resolved != dest_resolved and dest_resolved not in resolved.parents:
                    return JSONResponse({"error": "archive has unsafe paths"}, status_code=400)
            z.extractall(dest)
        return JSONResponse({"ok": True, "path": str(dest)})
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class ZiplistIn(BaseModel):
    path: str = ""


@router.post("/ziplist")
def ziplist(body: ZiplistIn) -> JSONResponse:
    """List a zip's members without extracting. Capped at 5000 entries."""
    enforce(body.path)
    if (d := _deny_secret(Path(body.path))):
        return d
    try:
        src = Path(body.path)
        if not src.is_file() or src.suffix.lower() != ".zip":
            return JSONResponse({"error": "not a zip"}, status_code=400)
        members: List[Dict[str, Any]] = []
        unsafe = 0
        with zipfile.ZipFile(src, "r") as z:
            for m in z.infolist():
                if len(members) >= 5000:
                    break
                name = m.filename.strip().replace("\\", "/").lstrip("/")
                if not name or ".." in Path(name).parts:
                    unsafe += 1
                    continue
                try:
                    mt = time.mktime(m.date_time + (0, 0, -1))
                except Exception:
                    mt = 0
                members.append({"name": name, "size": m.file_size,
                                "is_dir": m.is_dir(), "mtime": mt})
        return JSONResponse({"ok": True, "path": str(src), "members": members,
                             "total": len(members), "unsafe_skipped": unsafe})
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except zipfile.BadZipFile:
        return JSONResponse({"error": "bad zip"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class ZextractIn(BaseModel):
    path: str = ""
    member: str = ""
    dest_dir: str = ""


@router.post("/zextract")
def zextract_member(body: ZextractIn) -> JSONResponse:
    """Extract ONE zip member (file only) to a staging dir. Zip-slip guarded."""
    enforce(body.path)
    if body.dest_dir:
        enforce(body.dest_dir, write=True)
    if (d := _deny_secret(Path(body.path))):
        return d
    try:
        src = Path(body.path)
        if not src.is_file() or src.suffix.lower() != ".zip":
            return JSONResponse({"error": "not a zip"}, status_code=400)
        member = (body.member or "").strip().replace("\\", "/").lstrip("/")
        if not member or member.endswith("/") or ".." in Path(member).parts:
            return JSONResponse({"error": "bad member"}, status_code=400)
        dest = Path(body.dest_dir) if body.dest_dir else (
            Path(tempfile.gettempdir()) / "athena-zipview" / src.stem)
        if (d := _deny_secret(dest)):
            return d
        dest.mkdir(parents=True, exist_ok=True)
        dest_resolved = dest.resolve()
        with zipfile.ZipFile(src, "r") as z:
            try:
                info = z.getinfo(member)
            except KeyError:
                return JSONResponse({"error": "member not found"}, status_code=404)
            if info.is_dir():
                return JSONResponse({"error": "member is a folder"}, status_code=400)
            target = dest / member
            try:
                resolved = target.resolve()
            except Exception:
                return JSONResponse({"error": "bad archive entry"}, status_code=400)
            if resolved != dest_resolved and dest_resolved not in resolved.parents:
                return JSONResponse({"error": "archive has unsafe paths"}, status_code=400)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info, "r") as f, open(target, "wb") as out:
                shutil.copyfileobj(f, out, 1024 * 256)
        return JSONResponse({"ok": True, "path": str(target)})
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except zipfile.BadZipFile:
        return JSONResponse({"error": "bad zip"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


def _is_hidden(p: Path, entry_hidden: bool = False) -> bool:
    if p.name.startswith("."):
        return True
    if entry_hidden:
        return True
    try:
        attrs = p.stat().st_file_attributes  # Windows-only attr
        return bool(attrs & 0x2)
    except Exception:
        return False


def _entry(scan: os.DirEntry, show_hidden: bool) -> Optional[Dict[str, Any]]:
    try:
        is_dir = scan.is_dir(follow_symlinks=False)
    except Exception:
        return None
    try:
        st = scan.stat(follow_symlinks=False)
    except Exception:
        return None
    path = str(Path(scan.path))
    hidden = scan.name.startswith(".")
    if not hidden:
        try:
            hidden = bool(st.st_file_attributes & 0x2)
        except Exception:
            pass
    if hidden and not show_hidden:
        return None
    ext = "" if is_dir else Path(scan.name).suffix.lower()
    return {
        "name": scan.name,
        "path": path,
        "is_dir": is_dir,
        "size": 0 if is_dir else st.st_size,
        "mtime": st.st_mtime,
        "ext": ext,
        "hidden": hidden,
    }


@router.post("/roots")
def roots() -> Dict[str, Any]:
    """Quick access for an app browser: Athena's home, each installed app, each fixed disk.

    `roots` is what may actually be opened, `drives` is the same disks with their free space for
    callers that draw them. The panel has been drawing these as shortcuts since the port and they
    answered 403 every time — this is the half that made them mean something.
    """
    home = _athena_home()
    apps: List[Dict[str, Any]] = []
    try:
        api = home / "api"
        if api.is_dir():
            for d in sorted(api.iterdir(), key=lambda p: p.name.lower()):
                if d.is_dir():
                    apps.append({"name": d.name, "path": str(d), "rel": f"api/{d.name}"})
    except Exception:
        pass
    drives = []
    try:
        import psutil
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
                drives.append({
                    "device": part.device, "mount": part.mountpoint,
                    "fstype": part.fstype, "percent": u.percent,
                    "total_gb": round(u.total / (1024 ** 3), 1),
                })
            except Exception:
                pass
    except Exception:
        pass
    return {"home": str(home), "root": str(home / "api"), "apps": apps, "drives": drives,
            "may_read": [str(r) for r in _read_roots()],
            "may_write": [str(r) for r in _write_roots()]}



@router.post("/list")
def list_dir(body: PathIn) -> JSONResponse:
    enforce(body.path)
    target = body.path or str(_athena_home())
    if (d := _deny_secret(Path(target))):
        return d
    try:
        entries: List[Dict[str, Any]] = []
        total = 0
        with os.scandir(target) as it:
            for scan in it:
                total += 1
                if len(entries) >= MAX_LIST:
                    continue
                e = _entry(scan, body.show_hidden)
                if e:
                    entries.append(e)
        parent = str(Path(target).parent) if Path(target).parent != Path(target) else None
        return JSONResponse({"path": target, "parent": parent, "entries": entries,
                             "total": total, "truncated": total > len(entries)})
    except FileNotFoundError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class StatIn(BaseModel):
    path: str = ""
    dir_size: bool = False


def _dir_size(p: Path, file_cap: int = 100000) -> Dict[str, Any]:
    total = 0
    files = 0
    dirs = 0
    capped = False
    try:
        for root, subdirs, names in os.walk(p):
            dirs += len(subdirs)
            for n in names:
                files += 1
                if files > file_cap:
                    capped = True
                    return {"bytes": total, "files": files, "dirs": dirs, "capped": capped}
                try:
                    total += (Path(root) / n).stat(follow_symlinks=False).st_size
                except Exception:
                    pass
    except Exception:
        pass
    return {"bytes": total, "files": files, "dirs": dirs, "capped": capped}


@router.post("/stat")
def stat_path(body: StatIn) -> JSONResponse:
    enforce(body.path)
    if (d := _deny_secret(Path(body.path or ""))):
        return d
    p = Path(body.path or "")
    try:
        st = p.stat(follow_symlinks=False)
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    except PermissionError:
        return JSONResponse({"ok": False, "error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)
    is_dir = p.is_dir()
    info: Dict[str, Any] = {
        "ok": True,
        "name": p.name,
        "path": str(p),
        "is_dir": is_dir,
        "size": 0 if is_dir else st.st_size,
        "mtime": st.st_mtime,
        "ctime": getattr(st, "st_ctime", 0),
        "ext": "" if is_dir else p.suffix.lower(),
    }
    try:
        info["hidden"] = _is_hidden(p)
    except Exception:
        info["hidden"] = False
    if is_dir and body.dir_size:
        info["dir_size"] = _dir_size(p)
    return JSONResponse(info)

# How a file's bytes become text.
#
# A NUL byte in the first kilobyte used to mean "binary, show nothing", which is
# true of a .png and wrong about every UTF-16 file. UTF-16 is what a lot of this
# machine writes: PowerShell's `>` redirect, `wsl --list --verbose` output, some
# .ini and .log files. Those previewed as binary here, so they could not be read
# at all — and the search route had decoded UTF-16 for as long as it had existed,
# so the same file was searchable and unreadable at the same time.
#
# The order below is the order the evidence is worth. A byte-order mark is a
# statement about the file, so it is believed. Failing that, NULs in every other
# byte mean a 16-bit codec, and which side they land on says which one. Only then
# does a NUL with no pattern mean binary. A file that decodes as UTF-8 without
# being asked is text whatever its bytes look like.
TEXT_SNIFF_BYTES = 8192

_BOMS = ((codecs.BOM_UTF32_LE, "utf-32-le"),
         (codecs.BOM_UTF32_BE, "utf-32-be"),
         (codecs.BOM_UTF8, "utf-8-sig"),
         (codecs.BOM_UTF16_LE, "utf-16-le"),
         (codecs.BOM_UTF16_BE, "utf-16-be"))


def _utf16_side(head: bytes) -> Optional[str]:
    """Which 16-bit codec the NULs point at, or None when they point at nothing.

    Little-endian text puts its ASCII on even offsets and the NUL that follows
    each character on the odd ones; big-endian is the mirror. Both sides carrying
    NULs is not text in either — it is binary that happens to be even length.
    """
    if len(head) < 8:
        return None
    even, odd = head[0::2], head[1::2]
    quarter = max(2, len(even) // 4)
    if even.count(0) == 0 and odd.count(0) >= quarter:
        return "utf-16-le"
    if odd.count(0) == 0 and even.count(0) >= quarter:
        return "utf-16-be"
    return None


def _decode16(raw: bytes, encoding: str) -> Optional[str]:
    """Decode a 16-bit file, tolerating a truncation that landed mid-character.

    The read stops at a byte cap, and a cap on UTF-16 text falls wherever it
    falls — often between a character's two bytes. That cut is this reader's own,
    not a property of the file, so the odd trailing byte is dropped and the rest
    decoded rather than the whole file being called unreadable.
    """
    for candidate in (raw, raw[:len(raw) - len(raw) % 2]):
        try:
            text = candidate.decode(encoding)
        except UnicodeDecodeError:
            continue
        return None if "\x00" in text else text
    return None


# Control characters that a text file really does contain. Everything else below
# 0x20 is a byte a person did not type: a file made of them decodes perfectly and
# is still not text.
_TEXT_CONTROLS = "\t\r\n\f"


def _looks_like_text(text: str) -> bool:
    """Whether decoded characters read as a file rather than as packed bytes.

    Conversion is not evidence. `bytes([0, 1]) * 200` is a valid UTF-16BE string
    of U+0001 repeated, and one byte value per byte size will always decode as
    *something* 16-bit — so a decoder that stops at "it did not raise" hands the
    panel a screen of control characters and calls it a preview. The test is
    cheap and the threshold is generous: real text has tabs and newlines and
    almost nothing else below 0x20.
    """
    sample = text[:TEXT_SNIFF_BYTES]
    if not sample:
        return True
    odd = sum(1 for ch in sample
              if (ord(ch) < 0x20 and ch not in _TEXT_CONTROLS) or ord(ch) == 0x7f)
    return odd <= max(2, len(sample) // 20)


def sniff_text(raw: bytes) -> Optional[tuple]:
    """(text, encoding) for bytes that are text, None for bytes that are not."""
    head = raw[:TEXT_SNIFF_BYTES]
    for bom, encoding in _BOMS:
        if head.startswith(bom):
            try:
                text = raw.decode(encoding)
            except UnicodeDecodeError:
                return None
            if text.startswith("\ufeff"):
                # The mark is a statement about the bytes, not part of the file:
                # decoded with an explicit codec it would sit in the preview as an
                # invisible zero-width character at the top of every read.
                text = text[1:]
            return None if "\x00" in text else _as_text(text, encoding)
    if b"\x00" in head:
        side = _utf16_side(head)
        if side is None:
            return None
        text = _decode16(raw, side)
        return None if text is None else _as_text(text, side)
    try:
        return _as_text(raw.decode("utf-8"), "utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return _as_text(raw.decode("latin-1"), "latin-1")
    except Exception:
        return None


def _as_text(text: str, encoding: str) -> Optional[tuple]:
    """The answer, once the characters have been judged as well as decoded."""
    return (text, encoding) if _looks_like_text(text) else None

@router.post("/read")
def read_text(body: ReadIn) -> JSONResponse:
    enforce(body.path)
    if (d := _deny_secret(Path(body.path))):
        return d
    try:
        cap = max(1, min(body.max_kb, 1024)) * 1024
        with open(body.path, "rb") as f:
            raw = f.read(cap + 1)
        truncated = len(raw) > cap
        raw = raw[:cap]
        sniffed = sniff_text(raw)
        if sniffed is None:
            return JSONResponse({"is_binary": True, "truncated": truncated})
        text, encoding = sniffed
        # `encoding` is reported because a fallback is an answer about the bytes,
        # not about the file: a latin-1 read of UTF-8 mojibake is worth saying out
        # loud, and it is what tells a caller the sniff gave up.
        return JSONResponse({"is_binary": False, "text": text,
                             "encoding": encoding, "truncated": truncated})
    except FileNotFoundError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@router.post("/thumb")
def thumb(body: ThumbIn) -> JSONResponse:
    enforce(body.path)
    if (d := _deny_secret(Path(body.path))):
        return d
    try:
        if Path(body.path).suffix.lower() not in IMAGE_EXTS:
            return JSONResponse({"error": "not an image"}, status_code=400)
        if os.path.getsize(body.path) > THUMB_SRC_CAP_MB * 1024 * 1024:
            return JSONResponse({"error": "too large"}, status_code=413)
        from PIL import Image
        size = max(32, min(body.size, 1600))
        key = _thumb_key(body.path, "img", size)
        hit = _thumb_get(key)
        if hit is not None:
            b64 = base64.b64encode(hit).decode("ascii")
            return JSONResponse({"data_url": f"data:image/jpeg;base64,{b64}",
                                 "cached": True})
        with Image.open(body.path) as im:
            im.draft("RGB", (size, size))
            im = im.convert("RGB")
            im.thumbnail((size, size))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=70)
        _thumb_put(key, buf.getvalue())
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return JSONResponse({"data_url": f"data:image/jpeg;base64,{b64}"})
    except FileNotFoundError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class ImgInfoIn(BaseModel):
    path: str = ""


@router.post("/imginfo")
def imginfo(body: ImgInfoIn) -> JSONResponse:
    enforce(body.path)
    if (d := _deny_secret(Path(body.path))):
        return d
    try:
        if Path(body.path).suffix.lower() not in IMAGE_EXTS:
            return JSONResponse({"ok": False, "error": "not an image"}, status_code=400)
        from PIL import Image
        with Image.open(body.path) as im:
            w, h = im.size
            fmt = im.format or ""
            exif: Dict[str, str] = {}
            try:
                raw = im.getexif()
                if raw:
                    if 271 in raw or 272 in raw:
                        exif["camera"] = f"{raw.get(271, '')} {raw.get(272, '')}".strip()[:120]
                    if 306 in raw:
                        exif["taken"] = str(raw[306])[:40]
            except Exception:
                pass
        return JSONResponse({"ok": True, "width": w, "height": h, "format": fmt,
                             "exif": exif, "bytes": os.path.getsize(body.path)})
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    except PermissionError:
        return JSONResponse({"ok": False, "error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/mkdir")
def mkdir(body: MkdirIn) -> JSONResponse:
    enforce(body.parent, write=True)
    name = (body.name or "").strip().rstrip(". ")
    if not name or any(c in name for c in '/\\:*?"<>|'):
        return JSONResponse({"error": "bad name"}, status_code=400)
    try:
        target = Path(body.parent) / name
        if (d := _deny_secret(target)):
            return d
        target.mkdir(parents=False, exist_ok=False)
        return JSONResponse({"ok": True, "path": str(target)})
    except FileExistsError:
        return JSONResponse({"error": "already exists"}, status_code=409)
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class WriteIn(BaseModel):
    path: str = ""
    text: str = ""
    max_kb: int = 1024


@router.post("/write")
def write_text(body: WriteIn) -> JSONResponse:
    """Overwrite a text file. Refuses dirs, symlinks, and files over the cap."""
    enforce(body.path, write=True)
    try:
        p = Path(body.path)
        if not p.is_file() or p.is_symlink():
            return JSONResponse({"error": "not a plain file"}, status_code=400)
        if (g := _guard_root(p)):
            return g
        if (d := _deny_secret(p)):
            return d
        cap = max(1, min(body.max_kb or 1024, 1024)) * 1024
        if p.stat().st_size > cap:
            return JSONResponse({"error": "file too large to edit here"}, status_code=413)
        data = (body.text or "").encode("utf-8")
        if len(data) > cap:
            return JSONResponse({"error": "new content too large"}, status_code=413)
        p.write_bytes(data)
        return JSONResponse({"ok": True, "path": str(p),
                             "size": len(data), "mtime": p.stat().st_mtime})
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class MkfileIn(BaseModel):
    parent: str = ""
    name: str = ""


@router.post("/mkfile")
def mkfile(body: MkfileIn) -> JSONResponse:
    """Create an empty file. Auto-numbers when the name is taken."""
    enforce(body.parent, write=True)
    name = _clean_name(body.name)
    if not name or any(c in name for c in BAD_NAME_CHARS):
        return JSONResponse({"error": "bad name"}, status_code=400)
    try:
        parent = Path(body.parent)
        if not parent.is_dir():
            return JSONResponse({"error": "bad parent"}, status_code=400)
        if (d := _deny_secret(parent)):
            return d
        target = _free_path(parent / name)
        target.touch(exist_ok=False)
        return JSONResponse({"ok": True, "path": str(target)})
    except FileExistsError:
        return JSONResponse({"error": "already exists"}, status_code=409)
    except PermissionError:
        return JSONResponse({"error": "access denied"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class WatchIn(BaseModel):
    path: str = ""


@router.post("/watch")
def watch_dir(body: WatchIn) -> JSONResponse:
    """Cheap change fingerprint for auto-refresh: dir mtime + child count.

    No per-file stats, so polling every few seconds stays cheap even in
    huge folders. The frontend refetches /list only when this changes.
    """
    enforce(body.path)
    try:
        # Athena's home, not `Path.home()`: an empty path is a caller saying nothing, and
        # answering a question nobody asked about the person's own folder was a real answer
        # about a folder they never named. Found by audit.
        target = Path(body.path) if body.path else _athena_home()
        if (d := _deny_secret(target)):
            return d
        if not target.is_dir():
            return JSONResponse({"error": "not a folder"}, status_code=400)
        try:
            with os.scandir(target) as it:
                count = sum(1 for _ in it)
        except (PermissionError, FileNotFoundError, OSError):
            return JSONResponse({"error": "access denied"}, status_code=403)
        return JSONResponse({"ok": True, "path": str(target),
                             "fingerprint": f"{target.stat().st_mtime_ns}:{count}"})
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class SearchIn(BaseModel):
    root: str = ""
    query: str = ""
    show_hidden: bool = False
    max_results: int = 200
    max_depth: int = 8


@router.post("/search")
def search_files(body: SearchIn) -> JSONResponse:
    """Recursive filename search under root. Name matches only (no content).

    Bounded: max_results (cap 1000), max_depth (cap 12), and a dir-scan
    budget so a drive-root search can't hang the gateway. Symlinked dirs
    are matched by name but never descended into (loop-proof).
    """
    enforce(body.root)
    q = (body.query or "").strip().lower()
    if len(q) < 2:
        return JSONResponse({"error": "query too short"}, status_code=400)
    try:
        root = Path(body.root)
        if not root.is_dir():
            return JSONResponse({"error": "bad root"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "bad root"}, status_code=400)
    if (d := _deny_secret(root)):
        return d
    max_results = max(1, min(body.max_results or 200, 1000))
    max_depth = max(0, min(body.max_depth or 8, 12))
    dir_budget = 20000
    out: List[Dict[str, Any]] = []
    truncated = False
    try:
        stack = [(str(root), 0)]
        while stack and len(out) < max_results:
            cur, depth = stack.pop()
            if dir_budget <= 0:
                truncated = True
                break
            dir_budget -= 1
            try:
                with os.scandir(cur) as it:
                    children = list(it)
            except (PermissionError, FileNotFoundError, OSError):
                continue
            for scan in children:
                try:
                    is_dir = scan.is_dir(follow_symlinks=False)
                except Exception:
                    continue
                e = _entry(scan, body.show_hidden)
                if e is None:
                    continue
                if q in scan.name.lower():
                    out.append(e)
                    if len(out) >= max_results:
                        truncated = True
                        break
                if is_dir and depth < max_depth:
                    try:
                        if not os.path.islink(scan.path):
                            stack.append((scan.path, depth + 1))
                    except Exception:
                        pass
        return JSONResponse({"ok": True, "root": str(root), "query": body.query,
                             "entries": out, "total": len(out),
                             "truncated": truncated})
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


class GrepIn(BaseModel):
    root: str = ""
    pattern: str = ""
    show_hidden: bool = False
    max_hits: int = 100
    max_depth: int = 8
    exts: List[str] = []


@router.post("/grep")
def grep_content(body: GrepIn) -> JSONResponse:
    """Recursive plain-text content search under root. Case-insensitive substring.

    Bounded: max_hits (cap 500), max_depth (cap 12), 20k dir budget,
    1MB per-file cap, binary files skipped. Symlinked dirs never descended.
    Secret paths are denied at the root and pruned during the walk.
    """
    enforce(body.root)
    q = (body.pattern or "").strip().lower()
    if len(q) < 2:
        return JSONResponse({"error": "pattern too short"}, status_code=400)
    try:
        root = Path(body.root)
        if not root.is_dir():
            return JSONResponse({"error": "bad root"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "bad root"}, status_code=400)
    if (d := _deny_secret(root)):
        return d
    max_hits = max(1, min(body.max_hits or 100, 500))
    max_depth = max(0, min(body.max_depth or 8, 12))
    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in (body.exts or []) if e}
    dir_budget = 20000
    hits: List[Dict[str, Any]] = []
    files_scanned = 0
    truncated = False
    try:
        stack = [(str(root), 0)]
        while stack and len(hits) < max_hits:
            cur, depth = stack.pop()
            if dir_budget <= 0:
                truncated = True
                break
            dir_budget -= 1
            try:
                with os.scandir(cur) as it:
                    children = list(it)
            except (PermissionError, FileNotFoundError, OSError):
                continue
            for scan in children:
                if len(hits) >= max_hits:
                    truncated = True
                    break
                try:
                    is_dir = scan.is_dir(follow_symlinks=False)
                except Exception:
                    continue
                if is_dir:
                    if depth < max_depth:
                        try:
                            if os.path.islink(scan.path):
                                continue
                            if _deny_secret(Path(scan.path)):
                                continue
                            stack.append((scan.path, depth + 1))
                        except Exception:
                            pass
                    continue
                if not body.show_hidden and _is_hidden(Path(scan.path)):
                    continue
                if exts and Path(scan.name).suffix.lower() not in exts:
                    continue
                try:
                    if os.path.getsize(scan.path) > 1024 * 1024:
                        continue
                except Exception:
                    continue
                try:
                    with open(scan.path, "rb") as f:
                        chunk = f.read(65536)
                    if b"\x00" in chunk:
                        continue
                    text = chunk.decode("utf-8", errors="replace")
                except Exception:
                    continue
                files_scanned += 1
                lineno = 0
                for line in text.splitlines():
                    lineno += 1
                    if q in line.lower():
                        hits.append({"path": scan.path, "name": scan.name,
                                     "line": lineno, "text": line.strip()[:300]})
                        if len(hits) >= max_hits:
                            truncated = True
                            break
                # NOTE: only the first 64KB of each file is scanned (binary-safe,
                # keeps gateway memory flat on huge trees).
        return JSONResponse({"ok": True, "root": str(root), "pattern": body.pattern,
                             "hits": hits, "total": len(hits),
                             "files_scanned": files_scanned, "truncated": truncated})
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@router.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}
def _trash_parse_i(data: bytes) -> Optional[Dict[str, Any]]:
    """Parse a $I recycle-bin metadata file: size + deleted time + original path."""
    import struct
    try:
        if len(data) < 28:
            return None
        ver = struct.unpack("<q", data[0:8])[0]
        size = struct.unpack("<q", data[8:16])[0]
        ft = struct.unpack("<q", data[16:24])[0]
        # v1: path at 24; v2 (Win8+): 4-byte path-length field, path at 28.
        # NOTE: never search the raw bytes for b"\x00\x00" — the low byte of
        # the last char plus the terminator aliases as a false end. Decode
        # first (NUL is illegal in Windows paths), then cut at the NUL char.
        off = 28 if ver == 2 else 24
        orig = data[off:].decode("utf-16-le", "replace").split("\x00", 1)[0]
        deleted = max(0, (ft - 116444736000000000) // 10000000)
        return {"size": max(0, size), "deleted": deleted, "orig": orig}
    except Exception:
        return None


def _trash_roots() -> List[Path]:
    roots = []
    for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        rb = Path(f"{d}:\\$Recycle.Bin")
        try:
            if rb.is_dir():
                roots.append(rb)
        except Exception:
            pass
    return roots


def _trash_scan() -> List[Dict[str, Any]]:
    items = []
    for rb in _trash_roots():
        try:
            sids = [x for x in rb.iterdir() if x.is_dir()]
        except Exception:
            continue
        for sid in sids:
            try:
                inames = [x for x in sid.iterdir()
                          if x.is_file() and x.name.startswith("$I")]
            except Exception:
                continue
            for ip in inames:
                try:
                    meta = _trash_parse_i(ip.read_bytes())
                    if not meta or not meta["orig"]:
                        continue
                    rfile = ip.parent / ("$R" + ip.name[2:])
                    size = meta["size"]
                    try:
                        if rfile.exists():
                            size = rfile.stat().st_size if rfile.is_file() else size
                    except Exception:
                        pass
                    is_dir = False
                    try:
                        is_dir = rfile.is_dir()
                    except Exception:
                        pass
                    name = Path(meta["orig"]).name or ip.name
                    ext = "" if is_dir else Path(name).suffix.lower()
                    items.append({
                        "id": f"{sid.name}/{ip.name}",
                        "name": name,
                        "path": f"trash://{sid.name}/{ip.name}",
                        "orig": meta["orig"],
                        "deleted": meta["deleted"],
                        "size": size,
                        "mtime": meta["deleted"],
                        "is_dir": is_dir,
                        "ext": ext,
                    })
                except Exception:
                    continue
    items.sort(key=lambda x: -x["deleted"])
    return items


def _trash_resolve(item_id: str) -> Optional[Dict[str, Any]]:
    for it in _trash_scan():
        if it["id"] == item_id or it["path"] == item_id:
            return it
    return None


@router.post("/trash")
def trash_list() -> Dict[str, Any]:
    try:
        return {"ok": True, "items": _trash_scan()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "items": []}


@router.post("/trash-empty")
def trash_empty() -> Dict[str, Any]:
    removed = 0
    try:
        import ctypes
        SHERB_NOCONFIRMATION, SHERB_NOPROGRESSUI, SHERB_NOSOUND = 0x1, 0x2, 0x4
        rc = ctypes.windll.shell32.SHEmptyRecycleBinW(
            None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
        if rc == 0:
            return {"ok": True, "removed": -1}
    except Exception:
        pass
    for rb in _trash_roots():
        try:
            sids = [x for x in rb.iterdir() if x.is_dir()]
        except Exception:
            continue
        for sid in sids:
            try:
                files = [x for x in sid.iterdir() if x.is_file()]
            except Exception:
                continue
            for f in files:
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass
    return {"ok": True, "removed": removed}


class TrashRestoreIn(BaseModel):
    id: str = ""


@router.post("/trash-restore")
def trash_restore(body: TrashRestoreIn) -> Dict[str, Any]:
    # The item is looked up first: the check below reads the *recorded* path, so asking about an
    # item that does not exist used to raise on `None` and answer 500 rather than 404.
    it = _trash_resolve(body.id)
    if not it:
        return JSONResponse({"ok": False, "error": "item not found"}, status_code=404)
    # A restore writes the path the bin recorded, so the *recorded* path is what has to pass, not
    # the one in the request (which is a `trash://` id and names no destination at all).
    if it.get("orig"):
        try:
            enforce(it["orig"], write=True)
        except HTTPException as e:
            # Restoring is the one write whose destination the person did not just choose — they
            # chose it when they deleted the file. So the refusal says where the file came from
            # and what can still put it back, rather than reading as "Athena lost your file".
            detail = dict(e.detail) if isinstance(e.detail, dict) else {"error": str(e.detail)}
            detail["restore"] = (f"this file was deleted from {it['orig']}, a folder Athena "
                                 f"does not write to — Windows' own Recycle Bin restores it, and "
                                 f"Athena will not, so that a single click can never write onto a "
                                 f"system folder")
            return JSONResponse(detail, status_code=403)
    try:
        rest = body.id.split("trash://", 1)[1] if "trash://" in body.id else body.id
        sid_name, iname = rest.split("/", 1)
        rfile = None
        iname_r = "$R" + iname[2:] if iname.startswith("$I") else iname
        for rb in _trash_roots():
            cand = rb / sid_name / iname_r
            try:
                if cand.exists() or os.path.isdir(str(cand)):
                    rfile = cand
                    break
            except Exception:
                continue
        if rfile is None:
            return JSONResponse({"ok": False, "error": "backing file gone"}, status_code=410)
        dest = Path(it["orig"])
        if (d := _deny_secret(dest)):
            return d
        try:
            if not dest.parent.is_dir():
                return JSONResponse(
                    {"ok": False, "error": "original folder no longer exists"},
                    status_code=409)
        except Exception:
            return JSONResponse({"ok": False, "error": "bad original path"},
                                status_code=400)
        if dest.exists() or os.path.lexists(str(dest)):
            dest = _free_path(dest)
        os.rename(str(rfile), str(dest))
        try:
            (rfile.parent / iname).unlink()
        except Exception:
            pass
        return {"ok": True, "path": str(dest)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _os_clipboard_files() -> List[str]:
    """Read the Windows Explorer file clipboard (copy/paste outside Hermes)."""
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-Clipboard -Format FileDropList | ForEach-Object { $_.FullName }"],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return []
        return [ln.strip() for ln in (out.stdout or "").splitlines()
                if ln.strip() and os.path.exists(ln.strip())]
    except Exception:
        return []


@router.get("/dl")
def download_file(path: str = ""):
    """Direct file download for drag-out to Explorer (DownloadURL flavor).

    Files only, localhost callers. Folders can't ride DownloadURL —
    compress them first.
    """
    from fastapi.responses import FileResponse
    try:
        p = Path(path)
        # The router-level sandbox dependency does not run for an in-process
        # caller, so the same door is closed here as everywhere else.
        enforce([p])
        if not path or not p.is_file():
            return JSONResponse({"ok": False, "error": "file only"}, status_code=400)
        if (d := _deny_secret(p)):
            return d
        return FileResponse(str(p), filename=p.name)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


class CopyOutIn(BaseModel):
    paths: List[str] = []


@router.post("/copy-out")
def copy_out(body: CopyOutIn) -> Dict[str, Any]:
    """Put real files on the Windows clipboard so they paste into Explorer."""
    enforce(body.paths)
    paths = [p for p in body.paths
             if p and not p.startswith("trash://") and os.path.exists(p)]
    if not paths:
        return JSONResponse({"ok": False, "error": "nothing to copy"}, status_code=400)
    for p in paths:
        if _deny_secret(Path(p)):
            return JSONResponse({"ok": False, "error": "forbidden path"}, status_code=403)
    try:
        arr = ",".join(_ps_quote(p) for p in paths)
        cmd = ("Add-Type -AssemblyName System.Windows.Forms; "
               f"$c = New-Object System.Collections.Specialized.StringCollection; "
               f"$c.AddRange(@({arr})); "
               "[System.Windows.Forms.Clipboard]::SetFileDropList($c)")
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", cmd],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return JSONResponse(
                {"ok": False, "error": (out.stderr or "clipboard failed")[:200]},
                status_code=500)
        return {"ok": True, "count": len(paths)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


class PasteOsIn(BaseModel):
    dest: str = ""


@router.post("/paste-os")
def paste_os(body: PasteOsIn) -> Dict[str, Any]:
    """Paste files copied in Explorer into a FileBox folder."""
    enforce(body.dest, write=True)
    if not body.dest or body.dest.startswith("trash://"):
        return JSONResponse({"ok": False, "error": "bad destination"}, status_code=400)
    try:
        dest = Path(body.dest)
        if not dest.is_dir():
            return JSONResponse({"ok": False, "error": "bad destination"}, status_code=400)
        paths = _os_clipboard_files()
        if not paths:
            return JSONResponse({"ok": False, "error": "system clipboard has no files"},
                                status_code=404)
        return _paste("copy", OpIn(paths=paths, dest_dir=str(dest)))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)
