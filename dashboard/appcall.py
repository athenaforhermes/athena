"""Capability discovery and calling for installed Athena apps.

Every app in this store is one of two things — a Gradio UI or a ComfyUI graph —
and both of them describe themselves over HTTP once they are running. That is
the whole idea here: ask the app what it can do instead of shipping an adapter
per app.

What an app answers is not a tool list, though. RMBG-2-Studio reports 26 named
endpoints of which two are real capabilities; the rest are `update_preview` and
`lambda` handlers that drive its sliders. So the probe curates: drop the UI
plumbing, collapse numbered duplicates, rank what carries media in and out, and
give each survivor an intent a model can match a request against.

The result is saved next to the app as `agent.json` — the app's own instruction
sheet, written the first time it is probed and refreshed on demand.

`call` then runs one capability: it uploads local input files, posts the ordered
positional arguments Gradio expects, streams the result back, downloads every
file it produced into a run folder under Athena's home, and leaves a recipe
behind describing exactly what was done — which is also what makes a run
something a person can read afterwards and learn from.

Standard library only, on purpose: this module is loaded into the Hermes
backend process, where a missing import is a broken plugin.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
from pathlib import Path

try:
    import naming as athena_naming
except ImportError:  # loaded by path (a test or a tool), so its folder is not on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import naming as athena_naming
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA = 1
MANIFEST_NAME = "agent.json"

# Components that carry a media object rather than a value. An endpoint with one
# of these on either side is doing real work; the rest is usually UI plumbing.
MEDIA_COMPONENTS = {
    "image", "imageslider", "gallery", "file", "files", "video", "audio",
    "model3d", "imageeditor", "annotatedimage", "uploadbutton", "audioinput",
}
MULTI_COMPONENTS = {"files", "gallery"}

# A media signal in a label. Needed because Gradio describes a gallery return as
# type `array` with the label "Image Gallery (...)" — the component name is not
# in the type at all, so keying on components alone misses every gallery and the
# ranking then promotes sliders over the endpoint that does the work.
MEDIA_WORDS = re.compile(
    r"image|video|audio|gallery|file|photo|picture|frame|mask|model|3d|texture",
    re.IGNORECASE)


def _is_media(component: Any, type_: Any, label: Any) -> bool:
    c = str(component or "").lower()
    t = str(type_ or "").lower()
    lab = str(label or "")
    if c in MEDIA_COMPONENTS or t in MEDIA_COMPONENTS:
        return True
    return bool(MEDIA_WORDS.search(lab))

# An input that can be fed a local path: the caller uploads the file and passes
# back the server-side path. A Textbox cannot, which is why RMBG's URL-only
# single-image endpoint is not the one to call with a local file.
PATH_INPUT_COMPONENTS = {"image", "file", "files", "video", "audio", "model3d",
                         "gallery", "uploadbutton", "imageeditor", "annotatedimage"}

# Endpoint names that are UI plumbing by construction, not capability.
PLUMBING = (
    re.compile(r"^update_preview(_\d+)?$"),
    re.compile(r"^lambda(_\d+)?$"),
    re.compile(r"^reset_?.*$"),
    re.compile(r"^open_output_folder(_\d+)?$"),
    re.compile(r"^refresh.*$"),
    # Gradio names the handlers it wires to its own widgets with a leading
    # underscore — `/_on_remove` is "the button was clicked", not "remove
    # something". Gradio hides a leading underscore from its own API for exactly
    # this reason, and an app with 90 of them (WanGP) otherwise offers a model
    # hundreds of things to pick from, one of which is a widget event.
    re.compile(r"^_"),
    re.compile(r"^on_[a-z_]+$"),
)


def _is_plumbing(cap: Any) -> bool:
    """Is this endpoint a widget handler rather than something to ask for?"""
    name = str((cap or {}).get("name") or "").lstrip("/")
    return any(p.match(name) for p in PLUMBING)
OPAQUE_NAMES = {"fn", "main", "run", "process", "predict", "infer", "call", "do"}

# What a capability can be asked to do. Kept as a table rather than a regex so
# "removal" in an app's own description still resolves to the verb "remove".
_VERBS = {"remove", "delete", "erase", "cut", "crop", "generate", "create", "make",
          "upscale", "enhance", "restore", "colorize", "convert", "transcribe",
          "translate", "separate", "extract", "replace", "swap", "animate",
          "interpolate", "segment", "detect", "classify", "edit", "process", "render"}
_GENERIC = {"app", "tool", "support", "mode", "batch", "single", "multi", "mode"}

_FILE_KINDS = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".gif": "image", ".bmp": "image", ".tiff": "image",
    ".mp4": "video", ".webm": "video", ".mov": "video", ".mkv": "video",
    ".gifv": "video",
    ".wav": "audio", ".mp3": "audio", ".flac": "audio", ".ogg": "audio",
    ".m4a": "audio",
    ".glb": "model3d", ".gltf": "model3d", ".obj": "model3d", ".stl": "model3d",
    ".ply": "model3d", ".fbx": "model3d",
    ".zip": "archive", ".tar": "archive", ".gz": "archive",
    ".json": "data", ".csv": "data", ".txt": "text", ".md": "text",
}

# Requests are written in the caller's words, not the app author's. A request
# word that means the same thing as an intent word should count as one, or "cut
# out the subject" matches nothing while "remove the background" matches.
_SYNONYMS = {"cutout": "remove", "cut": "remove", "strip": "remove",
             "erase": "remove", "delete": "remove", "isolate": "remove",
             "matte": "remove", "knockout": "remove", "mask": "segment",
             "bg": "background", "backdrop": "background", "subject": "foreground",
             "picture": "image", "photo": "image", "pic": "image",
             "clip": "video", "movie": "video", "tune": "song", "music": "song",
             "voice": "audio", "sound": "audio", "speech": "audio"}

_LOCAL_HINTS = {"image", "photo", "picture", "video", "audio", "file", "clip",
                "song", "this", "local"}

_STOPWORDS = {
    "a", "an", "and", "app", "as", "at", "based", "by", "can", "for", "from",
    "in", "into", "is", "it", "its", "of", "on", "or", "the", "to", "with",
    "your", "you", "this", "that", "using", "powered", "built", "enhanced",
    "pinokio", "install", "files", "ui", "tabbed",
}


# --------------------------------------------------------------- http layer


class _AppRefused(Exception):
    """The app answered and refused — its own message is the useful part."""

    def __init__(self, message: str, raw: str = ""):
        self.raw = raw
        super().__init__(message)


class _HttpError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:200]}")


def _request(method: str, url: str, data: Optional[bytes] = None,
             headers: Optional[Dict[str, str]] = None,
             timeout: float = 60.0) -> Tuple[int, bytes, Dict[str, str]]:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            pass
        raise _HttpError(e.code, body.decode("utf-8", "replace")) from None


def _get_json(url: str, timeout: float = 60.0) -> Any:
    status, body, _ = _request("GET", url, timeout=timeout)
    if status >= 400:
        raise _HttpError(status, body.decode("utf-8", "replace"))
    return json.loads(body.decode("utf-8", "replace") or "null")


def _post_json(url: str, payload: Any, timeout: float = 120.0) -> Any:
    data = json.dumps(payload).encode("utf-8")
    status, body, _ = _request("POST", url, data=data,
                               headers={"Content-Type": "application/json"},
                               timeout=timeout)
    if status >= 400:
        raise _HttpError(status, body.decode("utf-8", "replace"))
    text = body.decode("utf-8", "replace")
    try:
        return json.loads(text or "null")
    except json.JSONDecodeError:
        return text


def _upload_urls(url: str) -> List[str]:
    """The route a file is POSTed to, newest form first, then the older bare one.

    Gradio 5 serves the upload only under `/gradio_api/` and answers the bare
    route with a 404; Gradio 4 is the other way round. Both are offered so the
    call site only has to name the app, and the generation is discovered rather
    than guessed.
    """
    u = str(url).rstrip("/")
    for suffix in ("/gradio_api/upload", "/upload"):
        if u.endswith(suffix):
            root = u[: -len(suffix)]
            return ([u, root + "/upload"] if suffix.startswith("/gradio_api")
                    else [root + "/gradio_api/upload", u])
    return [url]


def _multipart_upload(url: str, path: Path, timeout: float = 300.0) -> Any:
    """POST one file as Gradio's `files` field and return the parsed answer.

    Hand-rolled because the backend process cannot be assumed to have a
    multipart client, and a missing import here is a broken plugin. The route is
    resolved here rather than by the caller, so the same call site works whether
    the app is serving Gradio 5 (`/gradio_api/upload`) or Gradio 4 (`/upload`).
    """
    boundary = "----athena" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{path.name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = head + path.read_bytes() + tail
    last: Optional[Exception] = None
    for target in _upload_urls(url):
        try:
            status, resp, _ = _request(
                "POST", target, data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                timeout=timeout)
            if status >= 400:
                raise _HttpError(status, resp.decode("utf-8", "replace"))
            return json.loads(resp.decode("utf-8", "replace") or "null")
        except _HttpError as e:
            # A route this Gradio generation does not serve is not a failure —
            # the other form is tried. Anything else is the app's own answer.
            last = e
            if e.status in (404, 405):
                continue
            raise
    raise last if last else RuntimeError("upload failed")


def _pick_likely(files: List[Dict[str, Any]], inputs: List[Any]) -> Optional[str]:
    """Which returned file this run probably produced.

    An endpoint that answers with a gallery returns *everything* in the app's own
    output folder, so a run's results arrive mixed with files from earlier runs.
    A name carrying the input's stem is the one honest signal available over
    HTTP, and a single returned file needs no guessing at all.
    """
    if not files:
        return None
    if len(files) == 1:
        return files[0].get("path")
    stems = [Path(str(i)).stem.lower() for i in inputs
             if isinstance(i, str) and Path(str(i).replace("\\", "/")).is_file()
             and len(Path(str(i)).stem) >= 3]
    for f in files:
        name = str(f.get("name") or Path(str(f.get("path"))).name).lower()
        if any(s in name for s in stems):
            return f.get("path")
    return None


# Which file route this app answered on last time, keyed by app root. Gradio 5
# serves the download under `/gradio_api/file=` and 404s the bare `/file=` form;
# Gradio 4 is the reverse. Remembering the answer per root keeps a later download
# from re-probing the form that already failed — Wan2GP runs 5.29, where trying
# the bare form first costs seconds of retries on every file — and keying by root
# keeps one app's answer from being handed to another app on a different version.
_FILE_ROUTES: Dict[str, str] = {}


def _file_route_key(root: str) -> str:
    return str(root).rstrip("/")


def _remember_file_route(root: str, url: str) -> None:
    """Note the route family this app answered a download on, for the next file."""
    _FILE_ROUTES[_file_route_key(root)] = (
        "/gradio_api/file=" if "/gradio_api/file=" in url else "/file=")


def _file_urls(root: str, path: str) -> List[str]:
    """Where a produced file can actually be fetched from.

    The `url` an app hands back for a result is relative to the *call* route
    (`.../call/batch_pro/file=C:\\...`), which is not a fetchable address — the
    download then dies with a short read. The file route is the honest one, and
    Gradio 5 prefixes it, so both forms are offered. The form this app last
    answered on goes first; with nothing remembered, the prefixed form leads
    because that is what the newer Gradio generation serves.
    """
    q = urllib.parse.quote(str(path).replace("\\", "/"), safe="/:")
    bare, prefixed = f"{root}/file={q}", f"{root}/gradio_api/file={q}"
    return ([bare, prefixed] if _FILE_ROUTES.get(_file_route_key(root)) == "/file="
            else [prefixed, bare])


def _download(url: str, dest: Path, timeout: float = 300.0,
              attempts: int = 3) -> int:
    """Fetch one file, retrying a short read.

    A partially written file — the app still flushing a result when the request
    lands — closes the connection early and urllib reports an IncompleteRead.
    That is a retry, not a failure.
    """
    last: Optional[Exception] = None
    for n in range(attempts):
        try:
            status, body, _ = _request("GET", url, timeout=timeout)
            if status >= 400:
                raise _HttpError(status, body.decode("utf-8", "replace"))
            if not body:
                raise RuntimeError("empty response")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
            return len(body)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.6 * (n + 1))
    raise last if last else RuntimeError("download failed")


# ------------------------------------------------------------------ helpers


def _api_root(url: str) -> str:
    return str(url).rstrip("/")


def _port_of(url: str) -> Optional[int]:
    try:
        return urllib.parse.urlparse(url).port
    except ValueError:
        return None


def _elapsed(started: Any, finished: Any) -> Optional[int]:
    """Whole seconds between two `_now()` stamps, or None if either is unusable.

    None rather than 0 is deliberate: the notice only says how long a run took
    when it knows, so a run whose stamp is missing never reads as instant.
    """
    try:
        a = datetime.fromisoformat(str(started))
        b = datetime.fromisoformat(str(finished))
    except Exception:  # noqa: BLE001
        return None
    d = int((b - a).total_seconds())
    return d if d >= 0 else None


def _announce_run(run_dir: Path, recipe: Dict[str, Any],
                  result: Dict[str, Any]) -> None:
    """Tell every open Desktop window that a recorded run is over — with the artifact.

    An agent's call is not an engine job: it never passes through `_job_end`, so
    without this a twenty-minute render that ends while nobody is watching the
    panel is silent until somebody opens Runs and looks. The payload carries what
    came out, because "finished" is the uninteresting half of a finish notice.

    Best effort, like the job broadcast: by the time this runs the work is done
    and the record is on disk, so a missing or broken listener must not change
    the answer the caller gets.
    """
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
    except Exception:  # noqa: BLE001
        return
    files: List[Dict[str, Any]] = []
    for f in (result.get("files") or recipe.get("files") or []):
        if isinstance(f, dict) and f.get("path"):
            files.append({"name": f.get("name") or Path(str(f["path"])).name,
                          "path": str(f["path"]), "kind": f.get("kind") or "other",
                          "bytes": f.get("bytes")})
    payload = {
        "app": recipe.get("app") or run_dir.parent.name,
        "run": run_dir.name,
        "dir": str(run_dir),
        "intent": recipe.get("intent"),
        "capability": recipe.get("capability"),
        "state": recipe.get("state") or ("ok" if result.get("ok") else "failed"),
        "seconds": _elapsed(recipe.get("started"), recipe.get("finished")),
        "files": files,
        "likely_output": recipe.get("likely_output"),
        "error": recipe.get("error") or result.get("error"),
    }
    try:
        broadcast_plugin_event("athena", "run.done", payload)
    except Exception:  # noqa: BLE001
        pass


def _write_recipe(run_dir: Path, recipe: Dict[str, Any]) -> bool:
    """Write a run's recipe. Returns whether it landed.

    Written twice per run — once when it starts, once when it ends — so the
    record can be read while the job is still going.
    """
    try:
        (run_dir / "recipe.json").write_text(
            json.dumps(recipe, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _kind_of_file(name: str) -> str:
    return _FILE_KINDS.get(Path(str(name)).suffix.lower(), "other")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return re.sub(r"_+", "_", s)


def _installed_version(app_folder: Any, package: str) -> Optional[str]:
    """The version of a package inside the app's own environment.

    Gradio 4's `/info` answers with the endpoints and nothing else, so the app's
    installed distribution is the honest source for which major version is
    serving them — and that decides whether the call API is prefixed.
    """
    sites = list(Path(str(app_folder)).glob("app/env/Lib/site-packages"))
    sites += list(Path(str(app_folder)).glob("app/env/lib/python*/site-packages"))
    for site in sites:
        for d in site.glob(f"{package}-*.dist-info"):
            tail = d.name[len(package) + 1:-len(".dist-info")]
            return tail.split("-")[0]
    return None


def detect_kind(app_folder: Any) -> str:
    """gradio or comfyui, read from the app's own files. 'unknown' if neither."""
    folder = Path(str(app_folder))
    app = folder / "app"
    if (app / "comfyui_version.py").exists() and (app / "execution.py").exists():
        return "comfyui"
    if (app / "nodes.py").exists() and (app / "server.py").exists():
        return "comfyui"
    for f in sorted(app.glob("*.py"))[:40]:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if "gr.Blocks" in text or "import gradio" in text or "gradio as gr" in text:
            return "gradio"
    return "unknown"


# Nothing Athena shows or writes may carry the branding of the catalogue the
# apps came from. The app's own description is the usual way it leaks in.
_DEBRAND = [
    r"an?\s+enhanced\s+pinokio\s+app\s*(\(with\s+install\s+files\))?\s*built\s+on",
    r"an?\s+enhanced\s+pinokio\s+app\s*(\(with\s+install\s+files\))?",
    r"pinokio\s+app",
    r"pinokio",
]


def _debrand(text: Any) -> str:
    """The app's description with the catalogue's name taken out of it."""
    out = str(text or "")
    for pat in _DEBRAND:
        out = re.sub(pat, " ", out, flags=re.I)
    return re.sub(r"\s{2,}", " ", out).strip(" -–—:,")


def app_description(app_folder: Any) -> str:
    """The app's own words about itself — store metadata first, then README.

    Used to name an endpoint whose own name says nothing: RMBG's real
    background remover is called `fn`, and only the app's description tells you
    that. Reading it here keeps the intent honest instead of invented.
    """
    folder = Path(str(app_folder))
    parts: List[str] = []
    for meta in (*athena_naming.META_NAMES, "package.json"):
        p = folder / meta
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                text = (data.get("description") or data.get("title")
                        or data.get("name") or "")
                if text:
                    parts.append(_debrand(text))
                    break
            except Exception:  # noqa: BLE001
                pass
    for readme in ("README.md", "readme.md", "README.txt"):
        p = folder / readme
        if p.is_file():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if not s or s.startswith(("#", "!", "[", "<", "|", "```")):
                    continue
                # keep feature bullets: they are where an app names what it does
                parts.append(s.lstrip("-* ").strip())
                if len(" ".join(parts)) > 400:
                    break
            if parts:
                return _debrand(" ".join(parts))[:400]
    return ""


def _keywords(text: str) -> List[str]:
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", str(text).lower())
             if w and w not in _STOPWORDS and len(w) > 2]
    return words


def _intent_for(api_name: str, labels: Iterable[str], description: str,
                shape: str) -> Tuple[str, str]:
    """A short verb phrase a model can match a request against, plus its source.

    Order matters. A name the author chose is the best signal; a name like `fn`
    is worth nothing, so fall back to the app's own description ("Background
    Removal ...") and only then to a shape-based placeholder. The source is
    recorded so nobody has to guess where a name came from.
    """
    name = str(api_name).strip("/")
    base = re.sub(r"_\d+$", "", name)
    if base and base.lower() not in OPAQUE_NAMES and not re.fullmatch(r"[\d_]+", base):
        return _slug(base), "api_name"
    words = _keywords(description) or _keywords(" ".join(str(l) for l in labels))
    if words:
        verb = None
        for i, w in enumerate(words):
            base = re.sub(r"(al|ion|ing|ment|er|s)$", "", w)
            base = {"remov": "remove", "eras": "erase", "generat": "generate",
                    "creat": "create", "upscal": "upscale", "enhanc": "enhance",
                    "restor": "restore", "coloriz": "colorize", "transcrib": "transcribe",
                    "translat": "translate", "separat": "separate", "extract": "extract",
                    "replac": "replace", "animat": "animate", "interpolat": "interpolate",
                    "segment": "segment", "detect": "detect", "classif": "classify",
                    "conver": "convert", "cropp": "crop", "cut": "cut",
                    "process": "process", "edit": "edit", "swap": "swap"}.get(
                        base, base if base in _VERBS else "")
            if base in _VERBS:
                verb = base
                # "Background Removal" reads verb-first backwards: prefer the word
                # in front of the verb when there is one, which is where the
                # object of a noun-form verb sits in English prose.
                before = words[i - 1] if i > 0 else None
                after = words[i + 1] if i + 1 < len(words) else None
                noun = before if before and before not in _GENERIC else after
                if noun:
                    return f"{verb}_{noun}", "description"
                return f"{verb}_{shape}", "description"
    return f"{shape}", "shape"


def _shape_of(media_in: bool, media_out: bool) -> str:
    if media_in and media_out:
        return "transform_media"
    if media_out:
        return "generate_media"
    if media_in:
        return "analyze_media"
    return "run"


# What an endpoint is for, read from its own name. Gradio names the handlers it
# wires to its own widgets with a leading underscore; the one that starts the
# work says so in its name; a switch/toggle/reload handler only re-draws the
# form; and pause/abort only steer a run that is already going. Nothing is
# dropped for being none of these — the role is recorded so that a list which
# keeps every named endpoint stays readable.
_ROLE_GENERATE = re.compile(r"submit|generate|process|run|queue|start|add_task|render")
_ROLE_CONFIGURE = re.compile(r"^(switch|toggle|reload|refresh|update|load|seek)")
_ROLE_FORM = re.compile(r"_form|settings|config")
_ROLE_CONTROL = re.compile(r"pause|resume|abort|cancel|stop")


def _endpoint_role(api_name: str) -> str:
    """Which of the five kinds of endpoint a name declares itself to be."""
    name = str(api_name).lstrip("/")
    if name.startswith("_"):
        return "widget"
    # Control before generate on purpose: `pause_generation` contains "generate",
    # and a stop button reported as the app's generating capability is exactly
    # the confusion this field exists to end.
    if _ROLE_CONTROL.search(name):
        return "control"
    if _ROLE_GENERATE.search(name):
        if _ROLE_CONFIGURE.search(name) or _ROLE_FORM.search(name):
            return "configure"
        return "generate"
    return "other"


# ------------------------------------------------------------------- probe


def _gradio_config(root: str, timeout: float = 60.0) -> Dict[str, Any]:
    """The app's component graph, where the widget behind a parameter lives.

    `/info` names an endpoint's parameters but not the widget each one is, so a
    dropdown arrives with no choices and a slider with no range — and Gradio
    fabricates `param_55` where the app declared "Control Video". `/config` has
    the widgets. Gradio 5 serves it under the api prefix and Gradio 4 does not,
    so both are tried; a miss returns `{}` rather than raising, because a probe
    that cannot read the richer description still has `/info` to work with.
    """
    for path in ("/config", "/gradio_api/config"):
        try:
            data = _get_json(root + path, timeout=timeout)
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            continue
    return {}


def _config_index(config: Optional[Dict[str, Any]]
                  ) -> Tuple[Dict[Any, Dict[str, Any]], Dict[str, List[Any]]]:
    """Two lookups out of `/config`: a component's props, and an endpoint's inputs.

    A dependency lists its inputs as component ids in the same order as its
    parameters, which is the only join the two halves of the file offer. The
    api_name is kept without its leading slash so it matches the key `/info`
    uses for the same endpoint.
    """
    props: Dict[Any, Dict[str, Any]] = {}
    for c in (config or {}).get("components") or []:
        if isinstance(c, dict) and isinstance(c.get("props"), dict):
            props[c.get("id")] = c["props"]
    by_api: Dict[str, List[Any]] = {}
    for d in (config or {}).get("dependencies") or []:
        if isinstance(d, dict) and d.get("api_name"):
            ins = d.get("inputs")
            by_api[str(d["api_name"]).strip("/")] = list(ins) if isinstance(ins, list) else []
    return props, by_api


def _gradio_spec(url: str, timeout: float = 60.0) -> Tuple[Dict[str, Any], str]:
    """The app's endpoint description. Gradio 5 prefixes it, Gradio 4 does not."""
    root = _api_root(url)
    last: Optional[Exception] = None
    for path in ("/gradio_api/info", "/info"):
        try:
            data = _get_json(root + path, timeout=timeout)
            if isinstance(data, dict):
                return data, root + path
        except Exception as e:  # noqa: BLE001
            last = e
    raise last or RuntimeError("no capability description available")


# Gradio fabricates a label when the app declared none: `parameter_<component
# id>` (a live Wan2GP probe answered `label='parameter_181'`), and older clients
# saw `param_<n>` / `arg<n>`. A fabricated label is a placeholder wearing the
# shape of information, so it is never shown as one.
_PLACEHOLDER_LABEL = re.compile(r"^(?:parameter|param|arg|input|value)_?\d+$",
                                re.IGNORECASE)


def _real_label(*candidates: Any) -> str:
    """The first candidate that is a real name rather than a Gradio placeholder.

    Empty when every candidate was blank or fabricated, so a caller can tell
    "this parameter has no name" from "this parameter is called `parameter_181`".
    """
    for c in candidates:
        s = str(c or "").strip()
        if s and not _PLACEHOLDER_LABEL.match(s):
            return s
    return ""


def _capability_from_spec(api_name: str, spec: Dict[str, Any], description: str,
                          config: Optional[Dict[str, Any]] = None
                          ) -> Optional[Dict[str, Any]]:
    params = spec.get("parameters") or []
    rets = spec.get("returns") or []

    # `/info` names the parameters but not the widget each one is, so a dropdown
    # arrives with no choices and a slider with no range. `/config` carries them,
    # and its dependency lists the input component ids in the same order as the
    # parameters — that order is the join, and with no `/config` there is none,
    # so everything below reads the widgets it found and says which it was.
    props, by_api = _config_index(config)
    widget_ids = by_api.get(str(api_name).strip("/"))
    # Gradio keeps State out of `/info` — its value comes from the session, not
    # from the caller — so a dependency that has one lists more input ids than the
    # endpoint has parameters. Dropping exactly those ids makes the join
    # one-to-one again, and the same list says where a session-read input sits, so
    # a call can put a filler back in at that position. Without this, every
    # endpoint on a stateful app reads as unjoined and every call to one is
    # rejected for too few arguments.
    slots = [str((props.get(cid) or {}).get("name") or "").lower()
             for cid in (widget_ids or [])]
    widgets = [cid for cid, slot in zip(widget_ids or [], slots) if slot != "state"]
    joined = widget_ids is not None and len(widgets) == len(params)
    data_shape = (["state" if s == "state" else "in" for s in slots]
                  if joined and "state" in slots else None)

    inputs = []
    for i, p in enumerate(params):
        if not isinstance(p, dict):
            continue
        comp = str(p.get("component") or "")
        ptype = (p.get("type") or {}).get("type") if isinstance(p.get("type"), dict) else p.get("type")
        widget = props.get(widgets[i]) if joined and i < len(widgets) else None
        widget = widget if isinstance(widget, dict) else {}
        choices = widget.get("choices")
        # The label, in order of how far it can be trusted: the widget `/config`
        # placed for this parameter (read only when the join is one-to-one, so a
        # mismatch cannot silently shift every label by one), then `/info`'s own
        # label, then the app's real parameter name. A fabricated
        # `parameter_<n>` is skipped at every step, and a parameter with no name
        # at all is marked unnamed rather than handed one it never had.
        label = _real_label(widget.get("label") if joined else None,
                            p.get("label"), p.get("parameter_name"))
        inputs.append({
            "name": _real_label(p.get("parameter_name")) or f"arg{len(inputs)}",
            "label": label,
            "unnamed": not label,
            "component": comp,
            "type": ptype or "",
            "default": p.get("parameter_default"),
            "required": p.get("parameter_default") is None,
            # What the caller may send. Gradio keeps a dropdown's options either
            # as plain values or as [label, value] pairs, and both are recorded
            # as they are declared; a slider's range comes from the component.
            "choices": choices if isinstance(choices, list) else None,
            "min": widget.get("minimum"),
            "max": widget.get("maximum"),
            "media": _is_media(comp, ptype, p.get("label")),
            "accepts_local_path": comp.lower() in PATH_INPUT_COMPONENTS,
            # A `File` component with type `array` is Gradio's multiple-file
            # input: it wants a LIST. Sending one path string makes the app throw
            # an event error, which reads like a broken call rather than a wrong
            # argument shape.
            "multi": comp.lower() in MULTI_COMPONENTS or str(ptype or "").lower() == "array",
        })
    outputs = [{
        "label": r.get("label") or f"out{i}",
        "type": (r.get("type") or {}).get("type") if isinstance(r.get("type"), dict) else r.get("type"),
        # The component matters as much on the way out as on the way in. Gradio
        # reports an Image return as type `filepath` with whatever label the app
        # chose — often just "Output" — so the component is the only signal that
        # says media came back. Judging by type and label alone scored RMBG's
        # actual capability as having no media output at all.
        "component": str(r.get("component") or ""),
        "media": _is_media(r.get("component"),
                           (r.get("type") or {}).get("type") if isinstance(r.get("type"), dict)
                           else r.get("type"),
                           r.get("label")),
    } for i, r in enumerate(rets) if isinstance(r, dict)]

    media_in = any(i["media"] for i in inputs)
    media_out = any(o["media"] for o in outputs)
    shape = _shape_of(media_in, media_out)
    intent, source = _intent_for(api_name, [i["label"] for i in inputs] +
                                 [o["label"] for o in outputs], description, shape)
    score = 0.0
    score += 3.0 if (media_in and media_out) else 0.0
    score += 1.0 if media_out else 0.0
    score += 1.0 if media_in else 0.0
    score += 0.5 if source == "api_name" else 0.0
    return {
        "name": api_name,
        # Which of the five kinds this endpoint is, so that keeping every named
        # endpoint alongside the curated list costs the reader nothing.
        "role": _endpoint_role(api_name),
        # `measured` only when `/config` actually placed this endpoint's widgets;
        # a parameter read from `/info` alone is never passed off as observed.
        "confidence": "measured" if widget_ids is not None else "inferred",
        "intent": intent,
        "intent_source": source,
        "description": "; ".join(
            [f"inputs: {', '.join(i['label'] or i['name'] for i in inputs) or 'none'}",
             f"outputs: {', '.join(o['label'] for o in outputs) or 'none'}"]),
        "inputs": inputs,
        "outputs": outputs,
        # One entry per input the app's own dependency declares, in order:
        # "in" for a value the caller sends, "state" for one the app reads back
        # from the session at that position. Recorded only when the join is
        # exact, and `None` when every input is the caller's, so a plain call is
        # unchanged and a stateful one can be placed correctly.
        "data_shape": data_shape,
        "media_in": media_in,
        "media_out": media_out,
        "accepts_local_files": any(i["accepts_local_path"] for i in inputs),
        "score": score,
    }


def _curate(caps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build the offer: drop the plumbing, collapse `foo`, `foo_1`, rank.

    The point is that a model should see two things it can ask for, not the 26
    handlers the app happens to expose. Ranking is by media shape and by name
    quality, so the endpoint that actually does the work comes first.

    This list is what the model is *offered* and ranks against — an offer, not a
    gate. Everything it drops is still a name the app answers and is kept whole
    in `endpoints`; `call` resolves against that record, so curation narrows
    what is shown without narrowing what can be run.
    """
    kept: List[Dict[str, Any]] = []
    for c in caps:
        if _is_plumbing(c):
            continue
        kept.append(c)
    # numbered near-duplicates: keep the richest of each family
    # A numbered family only collapses when the members really are the same
    # handler: `/fn` and `/fn_1` in RMBG-2-Studio are two different functions
    # (a URL-only removal and the composition tab's), and collapsing them by name
    # silently deletes a capability. The signature has to match too.
    families: Dict[Tuple, Dict[str, Any]] = {}
    for c in kept:
        sig = (re.sub(r"_\d+$", "", str(c["name"]).lstrip("/")),
               tuple((i["component"], i["type"]) for i in c["inputs"]),
               tuple(o["type"] or "" for o in c["outputs"]))
        prev = families.get(sig)
        if prev is None or (c["score"], len(c["inputs"])) > (prev["score"], len(prev["inputs"])):
            families[sig] = c
    out = list(families.values())
    # The offer is media-shaped: when anything here moves a file in or out, the
    # handlers that do not are noise a model has to read past, and cutting them
    # is how a 721-endpoint app becomes 86 choices. This trims the OFFER only.
    # Every endpoint cut here is still a name the app answers and is still
    # callable by name — `_resolve_capability` reads the full `endpoints` record,
    # not this list. Wan2GP's own generation trigger is one of the cut ones.
    real = [c for c in out if c["media_in"] or c["media_out"]]
    if real:
        out = real
    out.sort(key=lambda c: (-c["score"], str(c["name"])))
    # Opaque names in one app can derive the same intent from the same
    # description ("remove_background" twice). Suffix the loser with what makes
    # it different so a request still resolves to exactly one capability.
    seen: Dict[str, int] = {}
    for c in out:
        intent = c["intent"]
        if intent in seen:
            # Two capabilities can derive one intent from one description.
            # Distinguish them by what the caller has to hand: an agent reading
            # "remove_background_from_file" knows it can pass a file, whereas the
            # Gradio parameter name it would otherwise be suffixed with tells it
            # nothing at all.
            if c.get("accepts_local_files"):
                detail = "from_file"
            elif c["inputs"]:
                detail = "from_" + _slug(str(c["inputs"][0].get("component") or "input"))
            else:
                detail = _slug(str(c["name"]).lstrip("/"))
            c["intent"] = f"{intent}_{detail}" if detail else f"{intent}_{c['name'].strip('/')}"
        seen[c["intent"]] = 1
    return out


_COMFY_WEIGHT_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".gguf", ".sft")
# The three folders that decide whether ComfyUI can generate at all. An
# upscaler or a LoRA in an extra path is not evidence of that, so only these
# keys are followed — a block under any other key is not a weight folder here.
_COMFY_MODEL_KEYS = ("checkpoints", "unet", "diffusion_models")
# Every folder ComfyUI's own extra-paths file knows about. Only the three above
# decide whether generation is possible; the rest are mapped anyway so the file
# Athena writes gives the app the whole library, not just the part the probe
# reads. They are written only when the folder exists, so pointing at a library
# with no LoRAs does not create a mapping to a directory that is not there.
_COMFY_KNOWN_KEYS = ("checkpoints", "clip", "vae", "loras", "embeddings",
                     "upscale_models", "unet", "diffusion_models", "controlnet")


def _comfy_extra_roots(folder: Path) -> List[Path]:
    """Model folders outside the app, from ComfyUI's own extra-paths file.

    An app that has been told where this machine's weights live still has an
    empty `models/` of its own, so counting only inside the app answers "cannot
    generate" about an app that can — and that answer is the whole reason this
    probe exists. The file is read the way ComfyUI reads it (`utils.extra_config`):
    `base_path` resolves the relative values, and one value holds one path per
    line, so a block scalar is a list. Only the three weight folders this probe
    counts are followed; nothing else in the file changes the answer.
    """
    out: List[Path] = []
    for name in ("extra_model_paths.yaml", "extra_model_paths.yml"):
        cfg = folder / "app" / name
        if not cfg.is_file():
            continue
        try:
            lines = cfg.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:  # noqa: BLE001
            continue
        base: Optional[str] = None
        key: Optional[str] = None
        key_indent: Optional[int] = None

        def take(value: str) -> None:
            """One path, resolved the way ComfyUI would resolve it."""
            text = os.path.expanduser(os.path.expandvars(value.strip().strip('"').strip("'")))
            if not text:
                return
            if not os.path.isabs(text):
                text = os.path.join(base or str(cfg.parent), text)
            out.append(Path(os.path.normpath(text)))

        for raw in lines:
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            stripped = raw.split(" #", 1)[0].strip()
            if not stripped:
                continue
            indent = len(raw) - len(raw.lstrip())
            if key in _COMFY_MODEL_KEYS and key_indent is not None and indent > key_indent:
                take(stripped)                      # a block-scalar line: another path
                continue
            if ":" not in stripped.rstrip(":"):
                continue                            # a section name, or nothing we count
            name_part, _, value = stripped.partition(":")
            name_part = name_part.strip().lower()
            value = value.strip()
            key, key_indent = name_part, indent
            if name_part == "base_path":
                base = value or None
            elif name_part in _COMFY_MODEL_KEYS and value not in ("", "|", "|-", ">"):
                take(value)
        break                                       # the first file ComfyUI would find
    return out


def _app_kind(app_folder: Any) -> str:
    """What the app said it was when it was probed, or '' when it never said.

    Read from the app's own manifest rather than guessed from files on disk: an
    app that has never been probed is treated exactly as it was before this
    existed, so the verdict is only widened where the app declared itself.
    """
    try:
        raw = (Path(str(app_folder)) / "agent.json").read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return ""
    return str(data.get("kind") or "").strip().lower()


def _comfy_models(app_folder: Any) -> Dict[str, Any]:
    """Whether an app has anything to generate with. Asked before it is asked.

    ComfyUI keeps its weights in three named folders; nothing else does. Asking
    only ComfyUI's question of every app answered "cannot generate" for a Gradio
    app holding 17 GB of loaded weights in `app/ckpts` — so an app that declares
    itself something other than ComfyUI is asked where *it* keeps them, and a
    miss is reported as "not weighable this way", never as an absence.
    """
    folder = Path(str(app_folder))
    kind = _app_kind(folder)
    other = bool(kind) and kind != "comfyui"
    roots = [folder / "app" / "models" / "checkpoints",
             folder / "app" / "models" / "unet",
             folder / "app" / "models" / "diffusion_models"]
    if other:
        roots += [folder / "app" / "ckpts", folder / "ckpts",
                  folder / "app" / "models", folder / "models"]
    extra = _comfy_extra_roots(folder)
    found: List[Dict[str, Any]] = []
    for root in roots + extra:
        files: List[str] = []
        try:
            if root.is_dir():
                for f in root.rglob("*"):
                    if f.is_file() and f.suffix.lower() in _COMFY_WEIGHT_SUFFIXES:
                        files.append(f.name)
                        if len(files) >= 50:
                            break
        except Exception:  # noqa: BLE001
            continue
        found.append({"folder": str(root), "models": files,
                      "source": "extra_model_paths" if root in extra else "app"})
    total = sum(len(f["models"]) for f in found)
    # The verdict is worth little unless it names the way out. An app whose own
    # models/ is empty is nearly always an app on a machine whose weights live
    # somewhere else, and that has one standard answer — so the answer travels
    # with the verdict, as data the UI can turn into a button and an agent can
    # apply without being told what to do.
    step: Optional[Dict[str, Any]] = None
    if not total and not other:
        step = {
            "action": "point_models",
            "label": "point ComfyUI at a weights folder",
            "detail": "this app's own models/ is empty, so every workflow would "
                      "fail at run time; name a folder that already holds "
                      "checkpoints or unet weights and ComfyUI reads them there",
        }
    if total:
        note = "generation is possible"
    elif other:
        note = (f"not weighable this way — a {kind} app does not keep its weights "
                f"where ComfyUI does, so finding none there says nothing about "
                f"whether it can generate")
    else:
        note = ("no model weights present — ComfyUI cannot generate until a "
                "checkpoint is downloaded, and the probe reports that instead "
                "of failing at call time")
    return {
        "ready": total > 0,
        # False only for an app whose weights cannot be judged by this question.
        # A caller that names apps "cannot generate" must respect it, or it
        # repeats the false verdict this field exists to stop.
        "weighable": bool(total) or not other,
        "kind": kind,
        "count": total,
        "folders": found,
        "next_step": step,
        "note": note,
    }


def comfy_weight_counts(root: Path) -> Dict[str, int]:
    """How many weights a candidate *library* holds, per generating folder.

    The same three keys `_comfy_models` counts, asked of a folder someone might
    point an app at instead of an app itself. A library with checkpoints can
    generate; one holding only LoRAs cannot, so counting all of them together
    would recommend the wrong folder.
    """
    out: Dict[str, int] = {}
    for key in _COMFY_MODEL_KEYS:
        sub = root / key
        n = 0
        try:
            if sub.is_dir():
                n = sum(1 for f in sub.rglob("*")
                        if f.is_file() and f.suffix.lower() in _COMFY_WEIGHT_SUFFIXES)
        except Exception:  # noqa: BLE001
            n = 0
        out[key] = n
    return out


def _comfy_library_root(root: Path) -> Path:
    """The folder to name as `base_path`, given the one a person pointed at.

    Someone picking their own weights folder usually means the folder that
    *holds* checkpoints/, unet/ and the rest — but pointing straight at
    `.../models/checkpoints` is the same intent written one level deeper, and
    answering "no weights under there" to it would be a lie.
    """
    if (root.name.lower() in _COMFY_MODEL_KEYS
            and comfy_weight_counts(root.parent).get(root.name.lower(), 0)):
        return root.parent
    return root


def comfy_extra_paths_path(folder: Any) -> Path:
    """Where ComfyUI looks for the extra-paths file, for this app."""
    return Path(str(folder)) / "app" / "extra_model_paths.yaml"


def comfy_point_models(folder: Any, root: Any, force: bool = False) -> Dict[str, Any]:
    """Point a ComfyUI app at a weights library, and say what it reaches now.

    The app folder is a clean checkout, so its own `models/` is empty while every
    workflow it ships would run against weights that live elsewhere on this
    machine — the state that reads as "cannot generate" and that only ends when
    someone writes this file by hand. Upstream never ships
    `extra_model_paths.yaml` and gitignores both it and `models/`, so writing it
    changes nothing the repository owns and a re-install of the app will not
    clobber it.

    Refusing to overwrite is deliberate. A file already there is either the
    person's own mapping or an earlier run of this, and silently replacing a
    mapping that works with one that may name the wrong folder is worse than
    asking again with `force`. A folder holding no generating weights is refused
    outright: the point of the write is that the app can then generate, and a
    mapping to an empty library would only move the failure later.
    """
    f = Path(str(folder))
    cfg = comfy_extra_paths_path(f)
    base = _comfy_library_root(Path(str(root)))
    counts = comfy_weight_counts(base)
    if not any(counts.values()):
        return {"ok": False, "counts": counts,
                "error": f"no weights under {base.as_posix()} — expected one of "
                         f"{', '.join(_COMFY_MODEL_KEYS)} to hold model files"}
    if cfg.exists() and not force:
        return {"ok": False, "exists": True, "path": str(cfg),
                "error": f"{cfg.name} is already there; pass force to replace it"}
    keys = [k for k in _COMFY_KNOWN_KEYS if (base / k).is_dir()]
    lines = [
        "# Written by Athena. ComfyUI reads this file at startup to find weights",
        "# that live outside the app; upstream ships no such file and gitignores",
        "# it, so this is not part of the checkout and re-installing keeps it.",
        "athena_shared:",
        f"    base_path: {base.as_posix()}",
    ]
    lines += [f"    {k}: {k}" for k in keys]
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(cfg), "base_path": base.as_posix(),
            "keys": keys, "counts": counts, "models": _comfy_models(f)}


def _comfy_workflows(app_folder: Any) -> List[Dict[str, Any]]:
    """Bundled workflow templates become the capability list.

    ComfyUI has node graphs, not named capabilities, so there is nothing to
    infer an intent from at runtime. Its shipped workflow JSON files are the
    honest substitute: each one is a real, saved recipe with a name.
    """
    folder = Path(str(app_folder))
    out: List[Dict[str, Any]] = []
    for root in (folder / "app" / "user" / "default" / "workflows",
                 folder / "app" / "workflows"):
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*.json")):
            out.append({
                "name": str(f.relative_to(folder).as_posix()),
                "intent": _slug(f.stem),
                "intent_source": "workflow_file",
                "description": f"saved ComfyUI workflow: {f.stem}",
                "inputs": [], "outputs": [],
                "media_in": False, "media_out": True,
                "accepts_local_files": False,
                "score": 1.0,
                "workflow_path": str(f),
                "status": "requires a model checkpoint",
            })
            if len(out) >= 60:
                return out
    return out


def probe(home: Any, app_folder: Any, url: str, kind: Optional[str] = None,
          description: Optional[str] = None, timeout: float = 60.0) -> Dict[str, Any]:
    """Ask a running app what it can do and write the answer next to it.

    Never raises: an app that cannot be reached yields a manifest carrying the
    error, so the caller can report it instead of crashing.
    """
    folder = Path(str(app_folder))
    name = folder.name
    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "app": name,
        "kind": kind or detect_kind(folder),
        "url": _api_root(url) if url else "",
        "port": _port_of(url) if url else None,
        "probed": _now(),
        "description": (description or app_description(folder))[:400],
        "capabilities": [],
    }
    try:
        if not url:
            raise RuntimeError("the app is not running, so there is nothing to probe")
        if manifest["kind"] == "comfyui":
            info = None
            for path in ("/object_info",):
                try:
                    info = _get_json(_api_root(url) + path, timeout=timeout)
                    manifest["source"] = _api_root(url) + path
                    break
                except Exception as e:  # noqa: BLE001
                    manifest["error"] = str(e)
            nodes = len(info) if isinstance(info, dict) else 0
            manifest["nodes"] = nodes
            manifest["models"] = _comfy_models(folder)
            manifest["capabilities"] = _comfy_workflows(folder)
            if not nodes and not manifest["capabilities"]:
                manifest["error"] = manifest.get("error") or "no capability description"
        else:
            spec, source = _gradio_spec(url, timeout=timeout)
            manifest["source"] = source
            # Optional, and the last thing fetched: it only enriches what `/info`
            # already named, so an app whose `/config` a proxy does not serve
            # still gets every endpoint, marked `inferred` instead of `measured`.
            config = _gradio_config(_api_root(url), timeout=timeout)
            manifest["gradio_version"] = (spec.get("version")
                                         or _installed_version(folder, "gradio"))
            named = spec.get("named_endpoints") or {}
            raw = []
            for api_name, es in named.items():
                # An entry that is not a dict still names an endpoint the app
                # exposes, so it is probed as an endpoint that describes nothing
                # rather than dropped on the floor.
                cap = _capability_from_spec(api_name, es if isinstance(es, dict) else {},
                                            manifest["description"], config)
                if cap:
                    raw.append(cap)
            manifest["endpoints_seen"] = len(raw)
            # Every named endpoint is kept here, widget handlers and control
            # endpoints included. `capabilities` below is the curated offer; this
            # is what the app actually exposes, so nothing it named is lost —
            # `pause_generation` takes only state inputs and would otherwise
            # vanish from a probe that reports the app's own answer.
            manifest["endpoints"] = raw
            manifest["capabilities"] = _curate(raw)
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        if "404" in msg or "no capability description" in msg:
            # The app is up and answered — it simply has no endpoint that
            # describes what it can do, which is the normal shape of a web UI
            # that is not a Gradio app. Reporting that as an error would call a
            # working app broken, and offer nothing to do about it.
            manifest["capabilities"] = []
            manifest["note"] = ("this app is running and answers HTTP, but it does not "
                                "describe its capabilities over the wire (it is not a "
                                "Gradio app): there is nothing named to call, so open it "
                                "and use its own interface")
            manifest["answered"] = False
        else:
            manifest["error"] = msg
    try:
        if folder.is_dir():
            (folder / MANIFEST_NAME).write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
            manifest["manifest_path"] = str(folder / MANIFEST_NAME)
    except Exception as e:  # noqa: BLE001
        manifest["write_error"] = str(e)
    # The tutorial is written with the manifest, not on demand: an app that has
    # been probed once should never need probing again to be understood.
    try:
        manifest["guide_path"] = str(write_guide(home, folder, manifest))
    except Exception as e:  # noqa: BLE001
        manifest["guide_error"] = str(e)
    return manifest


GUIDE_NAME = "GUIDE.md"


def write_guide(home: Any, app_folder: Any, manifest: Dict[str, Any]) -> Path:
    """The per-app tutorial, written the moment an app is probed.

    An app that has just been installed should explain itself in the same breath
    as it becomes usable. The tool answers carry the same facts and are the thing
    to trust at call time; this is the version that survives a context window,
    sits beside the app, and can be read by a person learning how it works.
    """
    folder = Path(str(app_folder))
    name = folder.name[:-4] if folder.name.lower().endswith(".git") else folder.name
    kind = str(manifest.get("kind") or "?")
    caps = manifest.get("capabilities") or []
    lines: List[str] = [f"# {name}", ""]
    desc = _debrand(manifest.get("description") or "").strip()
    if desc:
        lines += [desc, ""]
    lines += [f"Athena id `{folder.name}` · speaks {kind} · "
              f"{manifest.get('url') or 'started with athena_launch'}", ""]
    if not manifest.get("probed") and not manifest.get("error"):
        lines += ["This app has been installed but has not been asked what it can do yet.",
                  "Ask with `athena_capabilities` (it starts the app and reads its own",
                  "description of itself) and this file is rewritten with the answers.", ""]
    if not caps:
        lines += ["No capability was found for this app — it may still be installing,",
                  "or it may have nothing to expose over HTTP.", ""]
    else:
        lines += ["## What it can do", "",
                  "| ask for this | capability | takes | gives back |",
                  "| --- | --- | --- | --- |"]
        for c in caps:
            takes = ", ".join(
                f"{i.get('name')}"
                + (" (a file from this machine)" if i.get("accepts_local_path") else "")
                for i in (c.get("inputs") or [])) or "nothing"
            kinds = {str(o.get("component") or o.get("type") or "")
                     for o in (c.get("outputs") or [])} - {"", "?"}
            gives = ", ".join(sorted(kinds)) or "a result"
            lines.append(f"| `{c.get('intent')}` | `{c.get('name')}` | {takes} | {gives} |")
        lines += ["", "## How to run one", "", "```",
                  f'athena_generate(app="{folder.name}", capability="{caps[0].get("name")}",',
                  '               inputs=["C:/path/to/input"])',
                  "",
                  f'athena_generate(request="{str(caps[0].get("intent") or "").replace("_", " ")}",',
                  '               inputs=["C:/path/to/input"])',
                  "```", "",
                  "Inputs go in the order the capability lists them, and a local file path is",
                  "uploaded for you. Results are downloaded into Athena's runs folder and the",
                  "recipe of the run is written beside them. The app is started if it is not",
                  "already running.", ""]
        order = [f"- `{c.get('name')}`: " + ", ".join(
            f"{i.get('name')}"
            + (" (file)" if i.get("accepts_local_path") else "")
            + ("" if i.get("required") else ", optional")
            for i in (c.get("inputs") or [])) or f"- `{c.get('name')}`: no inputs"
            for c in caps]
        lines += ["## Inputs, in order", ""] + order + [""]
    notes: List[str] = []
    if kind == "comfyui":
        models = manifest.get("models") or {}
        notes.append("ComfyUI has no plain call path: this app runs saved workflows, so a "
                     "capability here is a workflow rather than an endpoint.")
        if not models.get("any"):
            notes.append("No checkpoint or unet is present, so nothing can be generated "
                         "until a model is downloaded or selected.")
    if manifest.get("gradio_version"):
        notes.append(f"Answers on Gradio {manifest['gradio_version']}, so the call route is "
                     f"{'/call/' if str(manifest['gradio_version']).startswith('4') else '/gradio_api/call/'}"
                     "— handled for you.")
    if notes:
        lines += ["## Worth knowing", ""] + [f"- {n}" for n in notes] + [""]
    lines += ["---", f"Generated by Athena's capability probe from the app's own description of "
                     f"itself ({manifest.get('probed') or 'just now'}). Regenerate with"
                     " `athena_capabilities` and `refresh: true`.", ""]
    p = folder / GUIDE_NAME
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def static_manifest(home: Any, app_folder: Any) -> Dict[str, Any]:
    """Write what can be said about an app without starting it.

    Called when an app finishes installing, so the guide and the manifest exist
    the moment the app does. Capabilities are deliberately left empty and
    `probed` left None: the real list comes from the app itself over HTTP, and
    starting an app silently the second it installs would put a model on the GPU
    with nobody waiting for it. The first ask fills it in (see `load`).
    """
    folder = Path(str(app_folder))
    kind = detect_kind(folder)
    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "app": folder.name,
        "kind": kind,
        "url": "",
        "port": None,
        "probed": None,
        "description": app_description(folder)[:400],
        "capabilities": [],
    }
    if kind == "comfyui":
        manifest["models"] = _comfy_models(folder)
        manifest["capabilities"] = _comfy_workflows(folder)
        manifest["note"] = ("workflows are known; a workflow only runs once its model "
                            "weights are present")
    try:
        manifest["manifest_path"] = str(folder / MANIFEST_NAME)
        (folder / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        manifest["error"] = f"the manifest could not be written: {e}"
    try:
        manifest["guide_path"] = str(write_guide(home, folder, manifest))
    except Exception as e:  # noqa: BLE001
        manifest["guide_error"] = str(e)
    return manifest


def load(home: Any, app_folder: Any) -> Optional[Dict[str, Any]]:
    """The manifest saved next to the app, or None when there is not one yet."""
    p = Path(str(app_folder)) / MANIFEST_NAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    # Read through the same filter the probe writes with. A manifest saved
    # before a plumbing rule existed would otherwise keep offering a widget
    # handler as a capability — and answering a request with one.
    caps = data.get("capabilities")
    if isinstance(caps, list):
        kept = [c for c in caps if not _is_plumbing(c)]
        if len(kept) != len(caps):
            data["capabilities"] = kept
    if not (Path(str(app_folder)) / GUIDE_NAME).is_file():
        # An app probed before the guide existed still deserves one, and reading
        # the manifest is where that is discovered.
        try:
            data["guide_path"] = str(write_guide(home, app_folder, data))
        except Exception:  # noqa: BLE001
            pass
    return data


# -------------------------------------------------------------------- call


def _resolution_pool(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every endpoint the app named, whether or not the offer kept it.

    `capabilities` is the short, ranked offer a model is shown; it is not the
    set of things that can be run. `_curate` drops non-media handlers from the
    offer and collapses numbered families, so a real endpoint — Wan2GP's
    `process_prompt_and_add_tasks`, the trigger its generation button fires —
    never appears there, and resolving against the offer alone answers "no
    capability" for an endpoint the app named and will happily run. Every named
    endpoint is kept whole in `endpoints`, and its entries are the same dicts the
    offer points at, so an intent `_curate` refined is seen here too. A manifest
    written before that field existed has only its offer, which is all it
    recorded.
    """
    full = manifest.get("endpoints")
    if isinstance(full, list) and full:
        return full
    return manifest.get("capabilities") or []


def _resolve_capability(manifest: Dict[str, Any], capability: str) -> Optional[Dict[str, Any]]:
    want = str(capability).strip()
    caps = _resolution_pool(manifest)
    for c in caps:
        if want in (c.get("name"), c.get("intent")):
            return c
    slug = _slug(want)
    for c in caps:
        if _slug(c.get("intent") or "") == slug or _slug(c.get("name") or "") == slug:
            return c
    for c in caps:
        if slug and slug in _slug(c.get("intent") or ""):
            return c
    return None


def _file_data(path: str) -> Dict[str, Any]:
    """The wire form of a file input.

    Gradio does not accept a path string where a file object is declared: a
    multiple-File input is validated against `ListFiles`, so sending a string
    fails with `Input should be a valid list` and the app's own error never
    surfaces (it is in the app's console, not in the SSE frame). This is the
    shape its JavaScript client sends.
    """
    name = Path(str(path).replace("\\", "/")).name
    return {"path": str(path), "orig_name": name,
            "meta": {"_type": "gradio.FileData"}}


def _looks_like_path(value: Any) -> bool:
    return isinstance(value, str) and len(value) < 400 and (
        ":" in value[:4] or value.startswith("/") or value.startswith("\\"))


def _looks_like_url(value: Any) -> bool:
    return isinstance(value, str) and value.lower().startswith(("http://", "https://"))


def _fetch_to_temp(url: str, log: List[str]) -> Optional[Path]:
    """Fetch a URL into a scratch file, for an app that wants a file, not a link.

    This is the other half of `gradio_client.handle_file`: a media argument may
    be given as a URL as well as a path, and the app can only be handed an upload
    either way. A URL the app would have to fetch itself is fetched here instead,
    because the app's network is not this machine's. Never raises: a URL that
    cannot be read is reported and the caller sends the value through unchanged.
    """
    try:
        name = Path(urllib.parse.urlparse(url).path).name or "download"
        dest = Path(tempfile.mkdtemp(prefix="athena-fetch-")) / name
        size = _download(url, dest, timeout=120.0)
        log.append(f"fetched {url[:80]} -> {size} bytes")
        return dest
    except Exception as e:  # noqa: BLE001
        log.append(f"could not fetch {url[:80]}: {str(e)[:120]}")
        return None


def _collect_outputs(payload: Any, base_url: str) -> List[Dict[str, str]]:
    """Every file reference anywhere in the result, however deeply nested.

    Gradio answers with a shape that varies by component and version — a bare
    path, a dict with path and url, or a gallery of those — so walk it rather
    than guess.
    """
    found: List[Dict[str, str]] = []
    seen: set = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            path = node.get("path") or node.get("name")
            url = node.get("url")
            if isinstance(path, str) and path and _looks_like_path(path):
                key = (path, url or "")
                if key not in seen:
                    seen.add(key)
                    found.append({"path": path, "url": url or "",
                                  "size": node.get("size")})
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)
        elif isinstance(node, str) and _looks_like_path(node):
            if (node, "") not in seen:
                seen.add((node, ""))
                found.append({"path": node, "url": f"{base_url}/file={node}"})

    walk(payload)
    return found


# One session hash per app, reused for the life of the process. Gradio keys the
# state an endpoint reads on it, so an app that keeps its prompt in session state
# (Wan2GP's `/process_prompt_and_add_tasks` takes only the gallery tab and the
# model and reads the prompt back from state) only sees what an earlier call set
# if both calls carry the same hash. Randomising per call is what makes a
# stateful app look like it ignores its inputs.
_SESSIONS: Dict[str, str] = {}


def _session_hash(root: str) -> str:
    """The stable Gradio session for this app, created the first time it is used."""
    return _SESSIONS.setdefault(root, uuid.uuid4().hex)


# Which event route an app answered on last time, keyed by app root — the same
# per-app memory `_FILE_ROUTES` keeps for downloads. Gradio 5 serves a call's
# result over the session queue (`/queue/data`) and 404s the documented per-event
# stream (`/call/<api>/<event_id>`) with "Session not found"; Gradio 4 is the
# reverse. Probing the wrong family first is a whole round trip thrown away on
# every single call — seconds on Wan2GP — so the route that worked is remembered
# and tried first, and keying by root keeps one app's answer off another app on a
# different Gradio. Holds "queue" or "per_event"; absent until one has answered.
_EVENT_ROUTES: Dict[str, str] = {}


def _event_route_key(root: str) -> str:
    return str(root).rstrip("/")


def _parse_gradio_events(text: str, event_id: str
                         ) -> Tuple[Any, Optional[str], bool]:
    """Read a Gradio event stream into (payload, error, settled).

    Two shapes are on the wire and both are read. The documented protocol sends
    `event: complete` / `event: error` frames; Gradio 5's queue instead streams
    `data: {"msg": ..., "event_id": ...}` frames whose `process_completed`
    carries the result and whose `success: false` is the app's own refusal.
    `settled` says whether the stream actually answered — a stream that only
    reported progress, or an error naming a session the server never opened, has
    not, and the caller should try the other route rather than believe it.
    """
    event = None
    payload: Any = None
    error: Optional[str] = None
    settled = False
    for raw in str(text).splitlines():
        line = raw.strip()
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
            continue
        if not line.startswith("data:"):
            continue
        chunk = line.split(":", 1)[1].strip()
        if not chunk:
            continue
        try:
            data: Any = json.loads(chunk)
        except json.JSONDecodeError:
            data = chunk
        if isinstance(data, dict) and "msg" in data:
            # Gradio 5's queue frames. Every event of this session arrives on the
            # same stream, so only this call's completion is the answer.
            if event_id and data.get("event_id") not in (None, event_id):
                continue
            msg = str(data.get("msg") or "")
            if msg == "process_completed":
                settled = True
                if data.get("success"):
                    payload = (data.get("output") or {}).get("data")
                else:
                    # Gradio puts the app's own message in the app's console, not
                    # in the frame: `output.error` is usually null and only the
                    # title says anything. Say what is actually known.
                    out = data.get("output") or {}
                    title = str(data.get("title") or "")
                    error = str(out.get("error")
                                or ("the app's run failed" + (f" ({title})" if title
                                    and title.lower() != "error" else "")))
            elif msg == "unexpected_error":
                settled = True
                error = str(data.get("message") or "the app hit an unexpected error")
            continue
        if event == "complete":
            settled = True
            payload = data
        elif event == "error":
            settled = True
            error = str(data)
    return payload, error, settled


def _read_gradio_events(root: str, api: str, event_id: str, session: str,
                        timeout: float, log: List[str]) -> Tuple[Any, Optional[str]]:
    """Follow one call to its result, on whichever route this Gradio serves it.

    Both families are asked for — the documented per-event stream and Gradio 5's
    session queue — but the one this app answered on last time goes first, so a
    repeat call does not re-probe the route that already failed on it; the other
    is still tried, so a remembered route that stops working costs one miss
    rather than a wrong answer. With nothing remembered the queue leads, because
    Gradio 5.29 answers the per-event route with "Session not found" and trying
    it first throws a round trip away on every call.
    """
    q = urllib.parse.quote(session, safe="")
    per_event = [
        (f"{root}/gradio_api/call/{api}/{event_id}", True),
        (f"{root}/call/{api}/{event_id}", True),
    ]
    queue = [
        (f"{root}/gradio_api/queue/data?session_hash={q}", False),
        (f"{root}/queue/data?session_hash={q}", False),
    ]
    candidates = (per_event + queue
                  if _EVENT_ROUTES.get(_event_route_key(root)) == "per_event"
                  else queue + per_event)
    for url, per_event_route in candidates:
        try:
            status, body, _ = _request("GET", url, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            log.append(f"GET {url} -> {type(e).__name__}: {str(e)[:90]}")
            continue
        text = body.decode("utf-8", "replace")
        log.append(f"GET {url} -> {status}, {len(text)} bytes of events")
        payload, error, settled = _parse_gradio_events(text, event_id)
        if not settled:
            continue
        if error and per_event_route and "session not found" in error.lower():
            log.append("the per-event route did not know the session; trying the queue")
            continue
        # Only the family that actually answered is remembered, so a route that
        # merely refused on the app's behalf is never mistaken for the working one.
        _EVENT_ROUTES[_event_route_key(root)] = "per_event" if per_event_route else "queue"
        return payload, error
    return None, None


def _gradio_call(root: str, api: str, data: List[Any], timeout: float) -> Tuple[Any, List[str]]:
    """Run one Gradio endpoint and return its outputs, trying the newer route first.

    A call is two steps — POST the ordered arguments, then read the stream that
    answers them — tied together by a session hash that is stable per app. Gradio
    5 serves both steps under `/gradio_api/` and Gradio 4 serves them bare, so
    each falls back to the older form. `/api/` (the oldest synchronous shape) is
    the last resort, because an app pinned to an older Gradio is not an error.
    """
    log: List[str] = []
    session = _session_hash(root)
    body = {"data": data, "session_hash": session}
    for post in (f"{root}/gradio_api/call/{api}", f"{root}/call/{api}"):
        try:
            first = _post_json(post, body, timeout=min(timeout, 120))
        except _HttpError as e:
            log.append(f"POST {post} -> {e.status}")
            if e.status not in (400, 404, 405, 307, 308):
                raise
            continue
        except Exception as e:  # noqa: BLE001
            log.append(f"POST {post} -> {type(e).__name__}: {str(e)[:120]}")
            continue
        log.append(f"POST {post} -> {type(first).__name__}")
        event_id = first.get("event_id") if isinstance(first, dict) else None
        if not event_id:
            # An older Gradio answers the call route synchronously, with the
            # outputs already in the body rather than an id to stream.
            if isinstance(first, dict) and "data" in first:
                return first.get("data"), log
            continue
        log.append(f"POST {post} -> event {event_id}")
        payload, error = _read_gradio_events(root, api, str(event_id), session, timeout, log)
        if error:
            # The app understood the call and refused it. Retrying an older sync
            # route would only turn this into an HTTP 500 with no detail, so stop
            # here and hand back what the app actually said.
            raise _AppRefused(f"the app refused the call: {error}", error)
        if payload is not None:
            return payload, log
        log.append("the stream ended without a result")
    for legacy in (f"{root}/api/{api}", f"{root}/run/{api}"):
        try:
            payload = _post_json(legacy, body, timeout=timeout)
            log.append(f"POST {legacy} -> synchronous")
            if isinstance(payload, dict) and "data" in payload:
                return payload.get("data"), log
        except Exception as e:  # noqa: BLE001
            log.append(f"POST {legacy} -> {type(e).__name__}: {str(e)[:120]}")
    raise RuntimeError("no call route answered; tried " + "; ".join(log))


def call(home: Any, app_folder: Any, capability: str, inputs: Optional[List[Any]] = None,
         timeout: float = 900.0, manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run one capability against the running app and collect what it produced.

    Local files in `inputs` are uploaded first and the app is given the path it
    expects, so a model can hand over a path on this machine and get a result
    back. Every file the app returns is downloaded into a run folder under
    Athena's home, and a recipe is written beside them recording exactly what
    was asked for — the run is the tutorial.
    """
    folder = Path(str(app_folder))
    name = folder.name[:-4] if folder.name.lower().endswith(".git") else folder.name
    result: Dict[str, Any] = {"ok": False, "app": name, "capability": capability,
                             "inputs": list(inputs or []), "files": [], "log": []}
    man = manifest if isinstance(manifest, dict) else (load(home, folder) or {})
    if not man:
        result["error"] = ("no manifest for this app yet — probe it first "
                           "(the app has to be running for that)")
        return result
    root = _api_root(man.get("url") or "")
    if not root:
        result["error"] = "the manifest has no url; probe the running app again"
        return result
    kind = man.get("kind")

    if kind == "comfyui":
        result["error"] = ("ComfyUI capabilities are saved workflows, not named "
                           "endpoints; run one through its own UI or queue it with "
                           "the workflow file this manifest names")
        result["workflow"] = next((c for c in (man.get("capabilities") or [])
                                   if c.get("name") == capability), None)
        result["log"] = ["comfyui: no direct call path"]
        return result

    cap = _resolve_capability(man, capability)
    if cap is None:
        result["error"] = f"no capability '{capability}' in the manifest"
        result["available"] = [c.get("intent") for c in (man.get("capabilities") or [])]
        return result
    result["capability"] = cap.get("name")
    result["intent"] = cap.get("intent")

    values = list(inputs or [])
    spec_inputs = cap.get("inputs") or []
    prepared: List[Any] = []
    for i, value in enumerate(values):
        spec = spec_inputs[i] if i < len(spec_inputs) else {}
        # A media argument arrives as a path or a URL — `handle_file` accepts
        # both — and either way the app is handed an upload, never a local path
        # it cannot read. A URL is fetched first; anything else is sent as-is.
        local: Optional[Path] = None
        if isinstance(value, str) and spec.get("accepts_local_path"):
            if Path(value).is_file():
                local = Path(value)
            elif _looks_like_url(value):
                local = _fetch_to_temp(value, result["log"])
        if local is not None:
            try:
                up = _multipart_upload(root + "/upload", local, timeout=timeout)
                if isinstance(up, list) and up:
                    first = up[0]
                    server = first.get("path") if isinstance(first, dict) else first
                    result["log"].append(f"uploaded {local.name} -> {server}")
                    fdata = _file_data(str(server))
                    if spec.get("multi"):
                        prepared.append([fdata])
                    else:
                        prepared.append(fdata)
                    continue
                result["log"].append(f"upload of {local.name} returned {up!r}")
            except Exception as e:  # noqa: BLE001
                result["log"].append(f"upload failed for {local.name}: {str(e)[:150]}")
        prepared.append(value)
    result["sent"] = prepared

    # A Gradio queue wants one value per input its dependency declares, and
    # `/info` leaves State inputs out because their value comes from the session
    # rather than from the caller. The filler recorded at probe time goes back in
    # at each of those positions — without it the app rejects the call for too
    # few arguments, which reads as a broken capability. Applied only when the
    # shape's real-argument count matches what the caller gave, so a wrong number
    # of arguments is never quietly padded into place.
    argued = prepared
    shape = cap.get("data_shape")
    if (isinstance(shape, list) and shape.count("in") == len(prepared)
            and len(shape) > len(prepared)):
        args = iter(prepared)
        argued = [None if slot == "state" else next(args, None) for slot in shape]
        result["log"].append(
            f"the app's own input order needs {len(shape)} values, {len(prepared)} "
            f"from the caller and {len(shape) - len(prepared)} it reads from the "
            f"session: {shape}")

    # The run exists before the call does, so the work can be watched while it
    # happens instead of only after it finishes: the folder and a recipe marked
    # "running" are written first, and the same recipe is rewritten with the
    # outcome at the end. A long job is otherwise invisible until it is over.
    run_dir = (Path(str(home)) / "runs" / name /
               datetime.now().strftime("%Y%m%d-%H%M%S"))
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"could not create the run folder: {e}"
        return result
    started = _now()
    recipe: Dict[str, Any] = {
        "app": name, "kind": kind, "capability": cap.get("name"),
        "intent": cap.get("intent"), "input_spec": spec_inputs,
        "inputs": list(inputs or []), "sent": prepared,
        "state": "running", "started": started, "when": started, "url": root,
    }
    _write_recipe(run_dir, recipe)
    result["run_dir"] = str(run_dir)
    result["recipe"] = str(run_dir / "recipe.json")

    try:
        payload, log = _gradio_call(root, str(cap.get("name")).strip("/"), argued, timeout)
        result["log"].extend(log)
        result["raw"] = payload
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        raw = getattr(e, "raw", "")
        if raw:
            result["log"].append("app said: " + raw[:400].replace("\n", " | "))
        result["log"].append("call failed")
        recipe.update({"state": "failed", "error": result.get("error"),
                       "finished": _now()})
        _write_recipe(run_dir, recipe)
        _announce_run(run_dir, recipe, result)
        return result

    refs = _collect_outputs(payload, root)

    for n, ref in enumerate(refs):
        src = ref["path"]
        base = Path(str(src).replace("\\", "/")).name or f"output-{n}"
        dest = run_dir / base
        if dest.exists():
            dest = run_dir / f"{dest.stem}-{n}{dest.suffix}"
        candidates = _file_urls(root, src)
        given = str(ref.get("url") or "")
        # the app's own url is only usable when it points at a file route of its
        # own origin rather than at the call route
        if given.startswith(root) and "/file=" in given and "/call/" not in given:
            candidates.append(given)
        size = 0
        for url in candidates:
            try:
                size = _download(url, dest, timeout=timeout)
                _remember_file_route(root, url)
                break
            except Exception as e:  # noqa: BLE001
                result["log"].append(f"fetch failed for {base} at {url[:90]}: {str(e)[:90]}")
        if not size:
            continue
        declared = ref.get("size")
        if isinstance(declared, int) and declared and declared != size:
            result["log"].append(f"{base}: app declared {declared} bytes, fetched {size}")
        result["files"].append({
            "path": str(dest),
            "name": base,
            "bytes": size,
            "kind": _kind_of_file(base),
            "source": src,
        })

    likely = _pick_likely(result["files"], inputs)
    if likely:
        result["likely_output"] = likely
    if len(result["files"]) > 1 and likely:
        strays = len(result["files"]) - len([f for f in result["files"]
                                             if f["path"] == likely])
        if strays:
            result["log"].append(f"{strays} of {len(result['files'])} returned files were "
                                 "already in the app's output folder")
    result["ok"] = bool(result["files"]) or not refs
    result["run_dir"] = str(run_dir)
    if len(refs) > len(result["files"]):
        result["log"].append(f"{len(refs)} file(s) returned, {len(result['files'])} fetched")
    note = ("an app that reports its results as a gallery lists everything in its "
            "own output folder, so a returned file is not necessarily from this "
            "run — the recipe records what this run actually asked for")
    if result["files"]:
        result["note"] = note
    recipe.update({
        "files": result["files"],
        "likely_output": result.get("likely_output"),
        "state": "ok" if result.get("ok") else "empty",
        "finished": _now(),
        "note": result.get("note"),
    })
    if not _write_recipe(run_dir, recipe):
        result["log"].append("the recipe could not be written")
    _announce_run(run_dir, recipe, result)
    return result


# ---------------------------------------------------------------- what takes a file

# A capability says which *components* it wants; an input only says it takes a
# local path. Gradio's component name is what actually distinguishes "give me an
# image" from "give me anything": `Image`, `Video`, `Audio` and `Model3D` are
# typed, while `File`/`Files` are the app saying it does not care. So the
# component name is read as the kind of file it wants, and a file's own kind —
# from its suffix — is what gets matched against it.
COMPONENT_KINDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("model3d", ("model3d", "mesh", "vrm", "pointcloud", "point_cloud")),
    ("video", ("video",)),
    ("audio", ("audio", "microphone", "mic")),
    ("image", ("image", "gallery", "sketchpad", "mask")),
    ("file", ("file", "files", "upload", "doc")),
)


def file_kind(name: Any) -> str:
    """What kind of file this is, by suffix: image, video, model3d, ... or other.

    The same map the caller uses to decide whether a path can be uploaded, named
    here so the question "will any app take this?" is asked with the same answer
    in both places.
    """
    return _kind_of_file(str(name or ""))


def _article(word: Any) -> str:
    """`a` or `an`, so a refusal reads like a sentence rather than a template."""
    return "an" if str(word or "")[:1].lower() in "aeiou" else "a"


def _component_kind(component: Any) -> Optional[str]:
    """Which kind of file a Gradio component wants, or None if it is not a path."""
    c = str(component or "").lower()
    if not c:
        return None
    for kind, hints in COMPONENT_KINDS:
        if any(h in c for h in hints):
            return kind
    # An input that accepts a local path but names no kind we recognise still
    # takes a file; it just does not get to claim it wants this kind of one.
    return "file" if c in {k.lower() for k in PATH_INPUT_COMPONENTS} else None


def fits(kind: str, cap: Dict[str, Any]) -> Dict[str, Any]:
    """Whether one capability would take a file of this kind, and where it goes.

    Pure, and deliberately narrow: it answers only what the manifest says. A
    capability that takes a URL instead of a path is not a candidate no matter
    how well its intent matches — offering it would send a person to an app that
    then asks them for a link.

    Returns `{ok, tier, score, input_index, input_name, multi, wants, reason}`
    where tier is `exact` (a typed input wants exactly this kind), `any-file`
    (the app takes a file and does not care which kind), or None.
    """
    if not isinstance(cap, dict):
        return {"ok": False, "tier": None, "score": 0.0, "reason": "not a capability"}
    inputs = [i for i in (cap.get("inputs") or []) if isinstance(i, dict)]
    paths = [(n, i) for n, i in enumerate(inputs) if i.get("accepts_local_path")]
    if not paths:
        wants_url = any(i.get("media") for i in inputs)
        return {"ok": False, "tier": None, "score": 0.0,
                "reason": ("it wants a link, not a file on this machine" if wants_url
                           else "it does not take a file")}
    best: Optional[Dict[str, Any]] = None
    for index, spec in paths:
        wants = _component_kind(spec.get("component")) or "file"
        if wants == kind and kind != "other":
            tier, score = "exact", 3.0
        elif wants == "file":
            # Generic on purpose: the app will accept whatever it is handed, so
            # this is the honest second place rather than a near miss.
            tier, score = "any-file", 1.0
        else:
            # A typed input that wants a different kind. Worth reporting as a
            # refusal with its reason — "it wants an image and this is a video"
            # is a more useful answer than silence.
            best = best or {"ok": False, "tier": None, "score": 0.0,
                            "input_index": index, "input_name": spec.get("name"),
                            "wants": wants,
                            "reason": f"it takes {_article(wants)} {wants} input, and this "
                          f"is {_article(kind)} {kind}"}
            continue
        if best is None or not best.get("ok") or score > best["score"]:
            best = {"ok": True, "tier": tier, "score": score,
                    "input_index": index, "input_name": spec.get("name"),
                    "multi": bool(spec.get("multi")), "wants": wants,
                    "reason": ("a " + wants + " input" if tier == "exact"
                               else "takes any file")}
        if tier == "exact":
            break
    return best or {"ok": False, "tier": None, "score": 0.0,
                    "reason": "it does not take a file"}


def _kinds_words(kinds: List[str]) -> str:
    """A list of kinds as someone would say them: "an image and a video"."""
    words = [_article(k) + " " + k for k in kinds if k]
    if not words:
        return "a file"
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def _cap_kinds(cap: Dict[str, Any]) -> List[str]:
    """The kinds a capability says it produces, from its own manifest."""
    out: List[str] = []
    for o in ((cap or {}).get("outputs") or []):
        if not isinstance(o, dict):
            continue
        kind = _component_kind(o.get("component") or o.get("type")) or ""
        # "file" is the app saying it hands back a file and not which kind, which
        # is exactly the case that cannot be checked up front. Leaving it out here
        # is what makes the chain say the handover was not promised.
        if kind and kind not in ("other", "file") and kind not in out:
            out.append(kind)
    return out


def _cap_wants(cap: Dict[str, Any]) -> List[str]:
    """The kinds of file a capability would take, "file" meaning it takes any."""
    out: List[str] = []
    for i in ((cap or {}).get("inputs") or []):
        if not isinstance(i, dict) or not i.get("accepts_local_path"):
            continue
        kind = _component_kind(i.get("component")) or "file"
        if kind not in out:
            out.append(kind)
    return out


def links(prev_cap: Dict[str, Any], next_cap: Dict[str, Any]) -> Dict[str, Any]:
    """Whether what one capability produces is what the next one takes.

    Pure, and answered from the two manifests alone — the same rule the routing
    question follows. `certain` records whether the manifests actually typed the
    handover: a chain whose first app does not say what it produces may run, but
    the log has to say it was not promised, because refusing a pipeline that
    would have worked is as bad as promising one that cannot.

    Returns `{ok, certain, kinds, wants, reason}`.
    """
    kinds = _cap_kinds(prev_cap)
    wants = _cap_wants(next_cap)
    if not kinds:
        return {"ok": True, "certain": False, "kinds": [], "wants": wants,
                "reason": "it does not say what it produces"}
    if not wants:
        return {"ok": False, "certain": True, "kinds": kinds, "wants": [],
                "reason": f"it produces {_kinds_words(kinds)} and the next one does "
                          f"not take a file"}
    matched = [k for k in kinds if k in wants]
    if "file" in wants:
        return {"ok": True, "certain": True, "kinds": kinds, "wants": wants,
                "reason": "the next one takes any file"}
    if matched:
        return {"ok": True, "certain": True, "kinds": kinds, "wants": wants,
                "reason": f"it takes {_kinds_words(matched)}"}
    return {"ok": False, "certain": True, "kinds": kinds, "wants": wants,
            "reason": f"it produces {_kinds_words(kinds)} and the next one takes "
                      f"{_kinds_words(wants)}"}



def candidates(kind: str, apps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Every installed capability that would take this kind of file, ranked.

    `apps` rows carry `{id, name, folder, manifest, running}` — the same shape
    `find_capability` takes, so a caller that has already read the manifests is
    not asked to read them twice. Only what the saved manifests say is used: an
    app that has never been probed is reported as such rather than guessed at,
    and a capability that takes a link is kept out of the list with the reason.

    Ranked by fit first, then by whether the capability produces media at all,
    and finally by app name so two runs of the same question give the same order.
    """
    out: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    unprobed: List[str] = []
    for app in apps or []:
        man = app.get("manifest") if isinstance(app.get("manifest"), dict) else None
        app_id = str(app.get("id") or app.get("name") or "")
        if not man or not (man.get("capabilities") or []):
            unprobed.append(app_id)
            continue
        for cap in man.get("capabilities") or []:
            verdict = fits(kind, cap)
            row = {"app": app_id, "app_name": app.get("name") or app_id,
                   "capability": cap.get("name"), "intent": cap.get("intent"),
                   "running": bool(app.get("running")),
                   "outputs_media": bool(cap.get("media_out")),
                   "reason": verdict.get("reason")}
            if not verdict.get("ok"):
                refused.append(dict(row, reason=verdict.get("reason")))
                continue
            out.append(dict(row, tier=verdict["tier"],
                            input_index=verdict.get("input_index"),
                            input_name=verdict.get("input_name"),
                            multi=verdict.get("multi"),
                            rank=round(verdict["score"]
                                       + (0.5 if cap.get("media_out") else 0.0)
                                       + min(0.4, float(cap.get("score") or 0.0) / 10.0), 3)))
    out.sort(key=lambda r: (-r["rank"], r["app_name"], str(r.get("capability"))))
    refused.sort(key=lambda r: (r["app_name"], str(r.get("capability"))))
    return {"kind": kind, "fits": out, "refused": refused, "unprobed": unprobed,
            "none": not out}


def find_capability(apps: List[Dict[str, Any]], request: str) -> Optional[Dict[str, Any]]:
    """Which installed app and capability matches a plain request.

    Matching is done on intents, which the probe wrote down in the app's own
    words, so "remove the background from this image" lands on the app whose
    intent is remove_background rather than on whichever app happens to be
    first. Exact intent wins; word overlap is the fallback.
    """
    words = {_SYNONYMS.get(w, w) for w in _keywords(request)}
    if not words:
        return None
    best: Optional[Tuple[float, Dict[str, Any]]] = None
    for app in apps or []:
        man = app.get("manifest") if isinstance(app.get("manifest"), dict) else None
        if man is None:
            man = load(app.get("home") or Path(app.get("path") or ".").parent.parent,
                       app.get("path"))
        if not man:
            continue
        for cap in man.get("capabilities") or []:
            intent = str(cap.get("intent") or "")
            if not intent:
                continue
            iwords = set(_keywords(intent))
            exact = 3.0 if _slug(intent) == _slug(request) else 0.0
            overlap = len(words & iwords) / max(1, len(iwords))
            # Only the words a request and an intent share say whether this is
            # the right capability. The bonuses below break a tie between
            # capabilities that already match; on their own they would let any
            # media capability answer any request at all.
            semantic = exact + overlap * 2.0
            if semantic <= 0.0:
                continue
            score = semantic + (0.3 if cap.get("media_in") else 0.0)
            # "remove the background from this image" implies a file on this
            # machine. Two of RMBG's capabilities carry the same intent and only
            # one can accept a local path (the other wants a public URL), so the
            # one that can take what the caller has should win.
            if cap.get("accepts_local_files") and (words & _LOCAL_HINTS):
                score += 1.5
            if score > 0 and (best is None or score > best[0]):
                best = (score, {"app": app.get("id") or app.get("name"),
                                "app_path": app.get("path"),
                                "app_running": app.get("running"),
                                "url": app.get("url"),
                                "capability": cap.get("name"),
                                "intent": intent,
                                "inputs": cap.get("inputs"),
                                "accepts_local_files": cap.get("accepts_local_files"),
                                "score": round(score, 3)})
    return best[1] if best else None
