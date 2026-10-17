"""`appcall.py`, without a network.

Athena's probe and caller are the two parts that talk to a *running* app, and
both are written against HTTP answers that are trivial to fake — which is the
point. What is worth testing is what Athena does with those answers: a Gradio
app reports 26 endpoints and two of them matter; a capability has to be called
with positional arguments in the order the app declared them; a run leaves a
recipe behind that a person can read afterwards. None of that needs a GPU, a
checkpoint, or an app that is actually up.

The HTTP layer is replaced, not stubbed out: `_get_json`, `_multipart_upload`,
`_download` and `_gradio_call` are swapped for functions that answer with the
shapes the real ones answer with, so everything between them is the real code.

Run with `env -u PYTHONPATH` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
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


ac = load("athena_appcall", PLUGIN / "dashboard" / "appcall.py")

home = Path(tempfile.mkdtemp(prefix="athena-appcall-"))
api_root = home / "api"
api_root.mkdir(parents=True)


def make_app(name: str, files: tuple[str, ...] = ()) -> Path:
    """A folder that looks like a real app to `detect_kind`.

    The content matters: kind is read from the app's own source — `import
    gradio` in something under app/, or ComfyUI's version and execution modules
    — so a file that says "x" is an app of no kind at all.
    """
    d = api_root / name
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        p = d / f
        p.parent.mkdir(parents=True, exist_ok=True)
        if f.endswith(".py"):
            p.write_text("import gradio as gr\nwith gr.Blocks() as demo:\n    pass\n",
                         encoding="utf-8")
        elif f.endswith((".html", ".js", ".json")):
            p.write_text("<!doctype html>\n" if f.endswith(".html") else "{}\n", encoding="utf-8")
        else:
            p.write_text("x\n", encoding="utf-8")
    return d


# ---------------------------------------------------------------- the answers
# The shape Gradio 5 answers `/gradio_api/info` with, using the parts of
# RMBG-2-Studio's real payload that matter: the capability that removes a
# background, and the slider/preview handlers that drive its own UI. Both of its
# "/fn" endpoints are here because they are two genuinely different functions
# with the same name shape — the case that must NOT be collapsed.
def gradio_info() -> dict:
    def param(component: str, type_: str, label: str, name: str, default=None):
        return {"component": component, "type": {"type": type_}, "label": label,
                "parameter_name": name, "parameter_default": default}

    img = param("Image", "numpy", "Input image", "input_image")
    box = param("Checkbox", "bool", "Return mask", "return_mask", False)
    num = param("Slider", "number", "Alpha", "alpha", 1.0)
    url = param("Textbox", "str", "Image URL", "image_url")
    text = param("Textbox", "str", "Text", "text")
    out = {"component": "Image", "type": {"type": "filepath"}, "label": "Output"}

    return {
        "version": "5.12.0",
        "named_endpoints": {
            # the two real capabilities
            "/remove_background": {"parameters": [img, box, num], "returns": [out]},
            "/fn": {"parameters": [img], "returns": [out]},
            # same name shape, different signature: a URL-only removal. Curating
            # by name alone would delete one of these two.
            "/fn_1": {"parameters": [url], "returns": [out]},
            # a Textbox the app labels "Image URL": an image goes in, but the app
            # fetches it itself, so a path from this machine is no use to it.
            "/remove_from_url": {"parameters": [url], "returns": [out]},
            # plumbing: drives the app's own sliders and previews
            "/update_preview": {"parameters": [img], "returns": [out]},
            "/lambda": {"parameters": [num], "returns": []},
            "/lambda_1": {"parameters": [num], "returns": []},
            "/lambda_2": {"parameters": [box], "returns": []},
            "/load_image": {"parameters": [], "returns": [out]},
            # a handler with no media anywhere: not a capability a model can use
            "/summarise": {"parameters": [text], "returns": [{"component": "Textbox", "type": "str"}]},
        },
        "unnamed_endpoints": {"/predict": {"parameters": [img], "returns": [out]}},
    }


real_get_json = ac._get_json


def fake_get_json(url: str, timeout: float = 60.0):
    if url.endswith("/gradio_api/info") or url.endswith("/info"):
        return gradio_info()
    raise RuntimeError("404 not found: " + url)


print("== a probe turns 9 endpoints into the ones that do work ==")
app = make_app("RMBG-2-Studio.git", ("app/app.py",))
ac._get_json = fake_get_json
man = ac.probe(home, app, "http://127.0.0.1:7860/")
ac._get_json = real_get_json

check("the probe stayed on the app that answered", man.get("app") == "RMBG-2-Studio.git", str(man.get("app")))
check("it is a gradio app", man.get("kind") == "gradio", str(man.get("kind")))
check("the port came out of the url", man.get("port") == 7860, str(man.get("port")))
check("the api root is recorded, not the page url", man.get("url") == "http://127.0.0.1:7860", str(man.get("url")))
check("every named endpoint was seen", man.get("endpoints_seen") == 10, str(man.get("endpoints_seen")))

caps = man.get("capabilities") or []
names = [c["name"] for c in caps]
check("the slider and preview handlers are gone",
      not any(n in names for n in ("/update_preview", "/lambda", "/lambda_1", "/lambda_2")), str(names))
check("the non-media handler is gone too (something here does media work)",
      "/summarise" not in names, str(names))
check("both same-named functions survive, because their signatures differ",
      "/fn" in names and "/fn_1" in names, str(names))
check("the real capability is found", "/remove_background" in names, str(names))
check("and it ranks first, being the richest",
      names[0] == "/remove_background", str(names))
urlish = next((c for c in caps if c["name"] == "/remove_from_url"), {})
check("an endpoint fed by a URL counts as media in",
      urlish.get("media_in") is True, str(urlish))
check("but is not offered a path from this machine",
      urlish.get("accepts_local_files") is False, str(urlish))

first = caps[0] if caps else {}
check("it knows an image goes in", first.get("media_in") is True, str(first.get("media_in")))
check("and a file comes out", first.get("media_out") is True, str(first.get("media_out")))
check("it knows the app will take a path from this machine",
      first.get("accepts_local_files") is True, str(first.get("accepts_local_files")))
check("the arguments keep the app's own order",
      [i["name"] for i in first.get("inputs") or []] == ["input_image", "return_mask", "alpha"],
      str([i["name"] for i in first.get("inputs") or []]))
check("an argument with a default is not required",
      [i["required"] for i in first.get("inputs") or []] == [True, False, False],
      str([i["required"] for i in first.get("inputs") or []]))
check("it carries an intent a model can match on",
      bool(first.get("intent")) and " " not in str(first.get("intent")),
      str(first.get("intent")))
ints = [c["intent"] for c in caps]
check("no two capabilities claim the same intent", len(ints) == len(set(ints)), str(ints))

print("\n== the answer is written down next to the app ==")
check("agent.json is saved in the app folder",
      (app / ac.MANIFEST_NAME).is_file() and ac.MANIFEST_NAME == "agent.json",
      str(app / ac.MANIFEST_NAME))
saved = json.loads((app / ac.MANIFEST_NAME).read_text(encoding="utf-8"))
check("and it is the same manifest that was returned",
      saved.get("capabilities") == caps, "differs")
check("a guide is written with it", (app / "GUIDE.md").is_file(), str(app / "GUIDE.md"))
check("the manifest says where its guide is",
      str(man.get("guide_path")) == str(app / "GUIDE.md"), str(man.get("guide_path")))
check("`load` reads back what the probe wrote",
      (ac.load(home, app) or {}).get("capabilities") == caps, "differs")

print("\n== an app that is not running is not an exception ==")
quiet = make_app("Quiet.git", ("app/app.py",))
m2 = ac.probe(home, quiet, "")
check("the probe returns instead of raising", isinstance(m2, dict), type(m2).__name__)
check("and says why", "not running" in str(m2.get("error")), str(m2.get("error")))
check("with no capabilities invented", m2.get("capabilities") == [], str(m2.get("capabilities")))
check("the manifest is still written, so the app can be understood later",
      (quiet / ac.MANIFEST_NAME).is_file())


def always_404(url: str, timeout: float = 60.0):
    raise RuntimeError("404 Client Error: Not Found for url: " + url)


ac._get_json = always_404
m3 = ac.probe(quiet, quiet, "http://127.0.0.1:7861/")
ac._get_json = real_get_json
check("an app that answers HTTP but describes nothing is not called broken",
      m3.get("error") is None and m3.get("answered") is False,
      f"error={m3.get('error')!r} answered={m3.get('answered')!r}")
check("and the note says what to do instead",
      "open it" in str(m3.get("note")), str(m3.get("note"))[:80])

print("\n== what kind of app is this, from the folder alone ==")
gradio_app = make_app("G.git", ("app/app.py",))
comfy_app = make_app("C.git", ("app/comfyui_version.py", "app/execution.py",
                               "app/workflows/default.json"))
static_app = make_app("S.git", ("index.html",))
check("a python web app is gradio-shaped", ac.detect_kind(gradio_app) == "gradio", ac.detect_kind(gradio_app))
check("an app with workflows is comfyui-shaped", ac.detect_kind(comfy_app) == "comfyui", ac.detect_kind(comfy_app))
check("a folder of html is none of those — it serves pages, it does not answer an API",
      ac.detect_kind(static_app) == "unknown", ac.detect_kind(static_app))

sm = ac.static_manifest(home, static_app)
check("a folder of pages gets a manifest without being probed",
      sm.get("capabilities") == [] and sm.get("probed") is None, str(sm)[:120])
check("and its kind says Athena does not know an API for it — no capability invented",
      sm.get("kind") == "unknown", str(sm.get("kind")))
check("the manifest was written into the app",
      (static_app / ac.MANIFEST_NAME).is_file(), str(static_app / ac.MANIFEST_NAME))
check("and a guide with it, so the app explains itself from the moment it exists",
      (static_app / "GUIDE.md").is_file(), str(static_app / "GUIDE.md"))
check("and the manifest carries its own schema", bool(sm.get("schema")), str(sm.get("schema")))

print("\n== a comfy app is not called through an endpoint ==")
(comfy_app / "agent.json").write_text(json.dumps(
    {"schema": sm.get("schema"), "app": "C.git", "kind": "comfyui",
     "url": "http://127.0.0.1:8188", "capabilities": [{"name": "wf", "intent": "generate"}]}),
    encoding="utf-8")
cout = ac.call(home, comfy_app, "wf")
check("the refusal points at the workflow instead of pretending to run",
      cout.get("ok") is False and "saved workflow" in str(cout.get("error")), str(cout.get("error")))
check("and hands back the workflow it means", (cout.get("workflow") or {}).get("name") == "wf",
      str(cout.get("workflow")))

print("\n== a comfy app counts the weights it was pointed at, not only its own ==")
# ComfyUI reads extra_model_paths.yaml at startup, and on a machine whose
# weights already live elsewhere that file is the difference between "cannot
# generate" and the truth. The probe has to read it the way ComfyUI does
# (utils.extra_config): base_path resolves the relative values, and one value
# holds one path per line, so a block scalar is a list rather than a string.
weights = home / "shared-models"
(weights / "checkpoints").mkdir(parents=True, exist_ok=True)
(weights / "checkpoints" / "flux-dev.safetensors").write_bytes(b"x" * 64)
(weights / "unet").mkdir(parents=True, exist_ok=True)
(weights / "unet" / "flux-dev-unet.safetensors").write_bytes(b"x" * 64)
linked = make_app("L.git", ("app/comfyui_version.py", "app/execution.py"))
(linked / "app" / "extra_model_paths.yaml").write_text(
    "mine:\n"
    f"    base_path: {weights.as_posix()}\n"
    "    checkpoints: checkpoints\n"
    "    is_default: false\n"
    "    diffusion_models: |-\n"
    "        unet\n"
    "        not-installed\n", encoding="utf-8")
lm = ac._comfy_models(linked)
sources = [f.get("source") for f in lm.get("folders") or []]
check("weights outside the app count as reachable",
      lm.get("ready") is True and lm.get("count") == 2,
      f"ready={lm.get('ready')} count={lm.get('count')}")
check("and the answer says which folders came from that file, not from the app",
      sources.count("extra_model_paths") == 3 and sources.count("app") == 3, str(sources))
check("a path named there but not on disk is listed empty rather than counted",
      [f.get("models") for f in lm.get("folders") or []
       if f.get("folder", "").endswith("not-installed")] == [[]],
      str([(f.get("folder"), f.get("models")) for f in lm.get("folders") or []]))
check("an app with no such file and empty folders still answers 'cannot generate'",
      ac._comfy_models(comfy_app).get("ready") is False, str(ac._comfy_models(comfy_app))[:120])

print("\n== 'cannot generate' carries the step that ends it, and applying the step works ==")
# A verdict nobody can act on is a dead end, and this app is not broken: its
# weights are somewhere else on the machine. So the verdict carries the write
# that fixes it, and applying that write answers with the re-checked verdict
# rather than a promise that a file was written.
empty = ac._comfy_models(comfy_app)
check("the verdict names the step, as data rather than a sentence",
      (empty.get("next_step") or {}).get("action") == "point_models", str(empty.get("next_step")))
check("and an app that can already generate carries none",
      ac._comfy_models(linked).get("next_step") is None,
      str(ac._comfy_models(linked).get("next_step")))

no_weights = home / "a-photo.png"
refused = ac.comfy_point_models(comfy_app, no_weights)
check("a folder with no generating weights is refused, so the failure is not moved later",
      refused.get("ok") is False and "no weights" in str(refused.get("error")),
      str(refused)[:160])

applied = ac.comfy_point_models(comfy_app, weights)
check("applying the step points the app at the library that has them",
      applied.get("ok") is True, str(applied)[:160])
check("and answers with the re-checked verdict, not just 'written'",
      (applied.get("models") or {}).get("ready") is True
      and (applied.get("models") or {}).get("count") == 2,
      str(applied.get("models"))[:150])
check("only folders that exist are mapped, so no mapping points at nothing",
      "checkpoints" in (applied.get("keys") or []) and "loras" not in (applied.get("keys") or []),
      str(applied.get("keys")))
athena_file = ac.comfy_extra_paths_path(comfy_app)
check("the file is the one ComfyUI reads, and it is not a file the checkout owns",
      athena_file.is_file() and athena_file.name == "extra_model_paths.yaml"
      and athena_file.parent.name == "app", str(athena_file))
check("the app's own probe now says it can generate",
      ac._comfy_models(comfy_app).get("ready") is True, str(ac._comfy_models(comfy_app))[:150])
second = ac.comfy_point_models(comfy_app, weights)
check("a second run refuses to replace an existing mapping instead of overwriting it",
      second.get("exists") is True and second.get("ok") is False, str(second)[:150])
check("and replaces it when asked to",
      ac.comfy_point_models(comfy_app, weights, force=True).get("ok") is True,
      str(ac.comfy_point_models(comfy_app, weights, force=True))[:150])
deeper = ac.comfy_point_models(comfy_app, weights / "checkpoints", force=True)
check("pointing straight at a checkpoints folder means the library that holds it",
      deeper.get("base_path") == weights.as_posix(), str(deeper.get("base_path")))
# Leave the fixture as it was found: the app is used again above this block and
# a mapping Athena wrote is not part of what this test set up.
athena_file.unlink()

print("\n== a call sends arguments in the app's order, and keeps what came back ==")
# The HTTP layer is replaced below the caller, so the caller itself is real:
# the upload of a local file, the positional payload, the run folder, the
# download of each returned file, and the recipe.
src_image = home / "a-photo.png"
src_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 400)

seen: dict = {}


def fake_upload(url: str, path: Path, timeout: float = 300.0):
    seen["upload"] = (url, Path(path).name)
    return [{"path": "/tmp/gradio/abc/a-photo.png", "name": Path(path).name}]


def fake_gradio_call(root: str, api: str, data: list, timeout: float):
    seen["api"] = api
    seen["data"] = data
    return ([{"path": "/tmp/gradio/def/out.png", "url": f"{root}/file=/tmp/gradio/def/out.png",
              "size": 12}], ["POST -> 200"])


def fake_download(url: str, dest: Path, timeout: float = 300.0, **kw):
    Path(dest).write_bytes(b"PNG!" * 4)
    seen.setdefault("downloads", []).append((url, Path(dest).name))
    return 16


ac._multipart_upload = fake_upload
ac._gradio_call = fake_gradio_call
ac._download = fake_download
res = ac.call(home, app, "remove_background", [str(src_image), True, 0.5])
ac._multipart_upload = real_get_json and ac._multipart_upload  # left swapped on purpose: see note

check("the call succeeded", res.get("ok") is True, str(res.get("error") or res.get("log")))
check("it called the endpoint the app named", seen.get("api") == "remove_background", str(seen.get("api")))
check("the local file was uploaded before the call",
      seen.get("upload") == ("http://127.0.0.1:7860/upload", "a-photo.png"), str(seen.get("upload")))
sent = seen.get("data") or []
check("the uploaded path is sent in the shape a File input expects",
      isinstance(sent[0], dict) and sent[0].get("path") == "/tmp/gradio/abc/a-photo.png", str(sent[:1]))
check("the remaining arguments follow in the app's declared order",
      sent[1:] == [True, 0.5], str(sent[1:]))
check("what came back was downloaded into the run folder",
      any(name == "out.png" for _url, name in seen.get("downloads") or []), str(seen.get("downloads")))
files = res.get("files") or []
check("and the result names it", bool(files) and files[0]["name"] == "out.png"
      and files[0]["bytes"] == 16, str(files))
check("a run folder exists under Athena's home",
      (home / "runs" / "RMBG-2-Studio").is_dir(), str(home / "runs"))
recipe_path = Path(str(res.get("recipe") or ""))
check("the recipe is written", recipe_path.is_file(), str(recipe_path))
recipe = json.loads(recipe_path.read_text(encoding="utf-8")) if recipe_path.is_file() else {}
check("it records the arguments that were sent, not just the ones asked for",
      recipe.get("sent") == sent, str(recipe.get("sent"))[:120])
check("it records the files this run produced",
      [f["name"] for f in recipe.get("files") or []] == ["out.png"], str(recipe.get("files"))[:120])
check("and it ends in a readable state, not 'running'",
      recipe.get("state") == "ok", str(recipe.get("state")))

print("\n== the finish notice carries the artifact ==")
# A run is not an engine job: it never passes through `_job_end`, so this
# broadcast is the only thing that tells a panel on another tab that a long call
# ended. What matters is that the payload carries what came out, and that a
# listener which is missing or broken cannot change the caller's answer.
seen: list[dict] = []


def _fake_events(name: str):
    """Stand in for hermes_cli.plugin_events, and for nothing being installed."""
    if name != "hermes_cli.plugin_events":
        raise ImportError(name)

    class _M:
        @staticmethod
        def broadcast_plugin_event(plugin, event, payload):
            seen.append({"plugin": plugin, "event": event, "payload": payload})
    return _M


sys.modules["hermes_cli.plugin_events"] = _fake_events("hermes_cli.plugin_events")
ac._announce_run(Path(str(res.get("run_dir"))), recipe, res)
ann = seen[-1] if seen else {}
p = ann.get("payload") or {}
check("a finished run is announced under its own event, not as a job",
      ann.get("event") == "run.done" and ann.get("plugin") == "athena", str(ann.get("event")))
# Named the way the Runs tab names it — the app's own folder, no ".git" suffix —
# so the notice and the row it points at read as the same app.
run_dir = Path(str(res.get("run_dir")))
check("the notice names the app and the run the same way the Runs tab does",
      p.get("app") == run_dir.parent.name and p.get("run") == run_dir.name
      and p.get("app") == recipe.get("app") and p.get("dir") == str(run_dir),
      f"{p.get('app')} / {run_dir.parent.name} / {p.get('run')}")
check("the notice carries the artifact, so a tab nobody is looking at can name it",
      [f["name"] for f in p.get("files") or []] == ["out.png"], str(p.get("files"))[:120])
check("and says which of the files is the one that was asked for",
      p.get("likely_output") == recipe.get("likely_output"),
      f"{p.get('likely_output')} / recipe {recipe.get('likely_output')}")
check("it reports the state the record ended in", p.get("state") == "ok", str(p.get("state")))
check("it knows how long the run took", isinstance(p.get("seconds"), int)
      and p["seconds"] >= 0, str(p.get("seconds")))

print("\n== a missing or broken listener is not the caller's problem ==")
del sys.modules["hermes_cli.plugin_events"]
before = len(seen)
try:
    ac._announce_run(Path(str(res.get("run_dir"))), recipe, res)
    landed = True
except Exception as e:  # noqa: BLE001
    landed = False
    print("   raised:", e)
check("with no event bus installed at all, announcing a run is a no-op",
      landed and len(seen) == before, f"landed={landed} seen={len(seen)}")


class _Exploding:
    @staticmethod
    def broadcast_plugin_event(*a, **k):
        raise RuntimeError("the bus is on fire")


sys.modules["hermes_cli.plugin_events"] = _Exploding
try:
    ac._announce_run(Path(str(res.get("run_dir"))), recipe, res)
    safe = True
except Exception:  # noqa: BLE001
    safe = False
check("and a listener that throws while being told cannot fail a finished run", safe)
del sys.modules["hermes_cli.plugin_events"]

print("\n== an unknown duration is left out, not rounded to zero ==")
check("two usable stamps give whole seconds",
      ac._elapsed("2026-10-14T10:00:00-04:00", "2026-10-14T10:01:00-04:00") == 60,
      str(ac._elapsed("2026-10-14T10:00:00-04:00", "2026-10-14T10:01:00-04:00")))
check("a missing stamp is None rather than 0, so nothing reads as instant",
      ac._elapsed(None, "2026-10-14T10:01:00-04:00") is None
      and ac._elapsed("2026-10-14T10:00:00-04:00", "") is None)
check("a backwards clock is refused too, not reported as negative time",
      ac._elapsed("2026-10-14T10:05:00-04:00", "2026-10-14T10:00:00-04:00") is None)

print("\n== an endpoint outside the curated offer is still callable by name ==")
# `capabilities` is the offer a model is shown, not the limit of what the app
# answers. `_curate` drops non-media handlers from the offer to keep it short —
# `/summarise` is one, dropped for exactly that reason — but it is still a name
# the app exposes and the probe recorded it in `endpoints`. A call path that
# reads only the offer answers "no capability" for an endpoint the app will run;
# that is the failure this guards, and it is the one that turned a real app's own
# generation trigger into "not in the manifest".
recorded = [c.get("name") for c in (man.get("endpoints") or [])]
check("the endpoint is in the recorded set", "/summarise" in recorded, str(recorded))
check("but not in the curated offer, so the two really do differ",
      "/summarise" not in names, str(names))
check("resolution reaches it through the full record, not the offer",
      (ac._resolve_capability(man, "summarise") or {}).get("name") == "/summarise",
      str(ac._resolve_capability(man, "summarise")))
off_seen: dict = {}


def fake_gradio_call_off(root: str, api: str, data: list, timeout: float):
    off_seen["api"] = api
    off_seen["data"] = data
    return (["a summary"], ["POST -> 200"])


_caller_gradio_call = ac._gradio_call
ac._gradio_call = fake_gradio_call_off
off = ac.call(home, app, "summarise", ["hello"])
ac._gradio_call = _caller_gradio_call
check("an uncurated endpoint is not refused as unknown through the public path",
      "no capability" not in str(off.get("error")), str(off.get("error")))
check("and it is called by the exact name the app exposed",
      off_seen.get("api") == "summarise", str(off_seen.get("api")))
check("with the argument the caller gave", off_seen.get("data") == ["hello"], str(off_seen.get("data")))
unknown = ac.call(home, app, "definitely_not_an_endpoint")
check("a name the app never exposed is still refused",
      unknown.get("ok") is False and "no capability" in str(unknown.get("error")),
      str(unknown.get("error")))

print("\n== asking for something the app does not have ==")
bad = ac.call(home, app, "make_coffee")
check("a missing capability is refused with the list of real ones",
      bad.get("ok") is False and "remove_background" in str(bad.get("available")), str(bad.get("available")))
no_manifest = ac.call(home, make_app("Fresh.git", ("app/app.py",)), "anything")
check("an app that was never probed says so instead of guessing",
      "probe it first" in str(no_manifest.get("error")), str(no_manifest.get("error")))

print("\n== a plain request lands on the right app and capability ==")
# The shape the app list carries: a manifest per app. `find_capability` matches
# against the capabilities the probe wrote down, in the app's own words.
shelf = [
    {"id": "wan.git", "manifest": {"capabilities": [{"name": "/t2v", "intent": "text_to_video"}]}},
    {"id": "RMBG-2-Studio.git", "manifest": {"capabilities": [
        {"name": "/remove_background", "intent": "remove_background",
         "media_in": True, "accepts_local_files": True},
        {"name": "/remove_from_url", "intent": "remove_background_from_url",
         "media_in": True, "accepts_local_files": False}]}},
]
hit = ac.find_capability(shelf, "remove the background from this image") or {}
check("the request resolves to the app that does that, not the first app on the shelf",
      hit.get("app") == "RMBG-2-Studio.git", str(hit.get("app")))
check("and to the capability that can take the file the caller has",
      hit.get("capability") == "/remove_background",
      str(hit.get("capability")))
check("a request nothing answers returns nothing rather than a wrong guess",
      ac.find_capability(shelf, "transcribe this podcast") is None,
      str(ac.find_capability(shelf, "transcribe this podcast")))

print()
if FAILURES:
    print(f"{FAIL} check(s) failed")
    for f in FAILURES:
        print("  FAIL", f)
else:
    print(f"{PASS}/{PASS} checks passed")
sys.exit(1 if FAIL else 0)
