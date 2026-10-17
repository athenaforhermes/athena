"""Which app would take this file — asked of the manifests, and nothing started.

Athena already asks every app what it can do, and saves the answer beside the app
as `agent.json`. This is the other direction: a person has a file and wants to
know who will take it. Almost everything worth checking here is about *not*
overclaiming, because a router that points at the wrong app wastes more of a
person's time than one that says it does not know:

  * a capability that takes a link is not a candidate, however well its intent
    matches — the person has a file, not a URL;
  * a capability whose typed input wants an image is not offered for a video, and
    the refusal says which is which;
  * an app that has never been probed is reported as unprobed rather than assumed
    to be able to do anything;
  * answering the routing question starts nothing: the whole point of reading
    `agent.json` instead of probing is that a model does not land on the card
    because somebody right-clicked a file;
  * the hand-over refuses everything it can before it makes a job — an unknown
    app, an unknown capability, and a capability whose required inputs are
    missing all come back as sentences, not as jobs that fail later.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
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


api = load("athena_api_under_test", PLUGIN / "dashboard" / "plugin_api.py")
ac = api._appcall_module()
check("the capability module loads through plugin_api", ac is not None)
if ac is None:
    print("t_accepts: 0 passed, 1 failed")
    sys.exit(1)

check("and it is the same module object the agent tools use, so one file map "
      "answers both halves", sys.modules.get("athena_appcall") is ac)


def cap(name="do_it", intent="do_this", component="Image", path=True, media=True,
        required=True, multi=False, media_out=True, score=9.0, inputs=None):
    """One capability in the shape the probe writes them."""
    if inputs is None:
        inputs = [{"name": "image", "component": component, "accepts_local_path": path,
                   "media": media, "required": required, "multi": multi}]
    return {"name": name, "intent": intent, "inputs": inputs, "media_in": media,
            "media_out": media_out, "accepts_local_files": any(i["accepts_local_path"]
                                                               for i in inputs),
            "score": score}


def app(app_id, caps, running=False, manifest=True, name=None):
    return {"id": app_id, "name": name or app_id, "folder": f"/x/{app_id}",
            "running": running,
            "manifest": {"capabilities": caps} if manifest else None}


# ------------------------------------------------------- what kind of file
print("\n== what kind of file this is ==")
for fname, want in [("a.png", "image"), ("A.JPEG", "image"), ("clip.mp4", "video"),
                    ("take.wav", "audio"), ("part.stl", "model3d"),
                    ("model.glb", "model3d"), ("bundle.zip", "archive"),
                    ("rows.csv", "data"), ("notes.md", "text"),
                    ("LICENSE", "other"), ("thing.xyzzy", "other")]:
    got = ac.file_kind(fname)
    check(f"  {fname} is {want}" + ("" if got == want else f" (got {got})"), got == want)
check("  a path works as well as a name, because the caller has one",
      ac.file_kind("/some/where/photo.png") == "image")

# --------------------------------------------------------- one capability
print("\n== one capability, one file ==")
exact = ac.fits("image", cap())
check("a typed image input takes an image, and says exactly which slot it goes in",
      exact["ok"] and exact["tier"] == "exact" and exact["input_index"] == 0
      and exact["wants"] == "image" and exact["input_name"] == "image", str(exact))
check("and it is not marked as wanting a list", exact["multi"] is False)

generic = ac.fits("image", cap(component="File"))
check("an app that takes any file is a candidate, second rank, and does not "
      "pretend to have wanted an image",
      generic["ok"] and generic["tier"] == "any-file", str(generic))

many = ac.fits("image", cap(component="Files", multi=True))
check("a multiple-file input is marked as one, because Gradio wants a list there "
      "and a single path makes it throw", many["ok"] and many["multi"] is True, str(many))

wrong = ac.fits("video", cap(component="Image"))
check("a typed input that wants an image does not take a video",
      not wrong["ok"] and wrong["wants"] == "image", str(wrong))
check("and the refusal says which is which, with the article a person would use",
      wrong.get("reason") == "it takes an image input, and this is a video",
      str(wrong.get("reason")))

url_only = ac.fits("image", cap(component="Textbox", path=False))
check("a capability that wants a link is not a candidate for a file on this machine",
      not url_only["ok"] and "link" in str(url_only.get("reason")), str(url_only))

nothing = ac.fits("image", cap(inputs=[], media=False))
check("a capability that takes no input at all is not a candidate",
      not nothing["ok"] and nothing.get("reason") == "it does not take a file",
      str(nothing))
check("and a malformed capability does not crash the router",
      not ac.fits("image", "not-a-capability")["ok"])

other_kind = ac.fits("other", cap(component="File"))
check("a file with no known suffix is still a candidate for an app that takes "
      "any file", other_kind["ok"] and other_kind["tier"] == "any-file", str(other_kind))
check("but it never claims to be what a typed input wanted",
      not ac.fits("other", cap(component="Image"))["ok"])

second = ac.fits("image", cap(inputs=[
    {"name": "prompt", "component": "Textbox", "accepts_local_path": False, "media": False},
    {"name": "picture", "component": "Image", "accepts_local_path": True, "media": True},
]))
check("the slot it names is the file slot, not the first input",
      second["ok"] and second["input_index"] == 1 and second["input_name"] == "picture",
      str(second))

print("\n== every app that would take it, ranked ==")
view = ac.candidates("image", [
    app("generic", [cap(name="any", intent="do_any", component="File", score=2.0)]),
    app("typed", [cap(name="bg", intent="remove_background", score=9.0)]),
    app("unread", [], manifest=False),
    app("empty", []),
    app("linky", [cap(name="from_url", intent="from_url", component="Textbox", path=False)]),
])
check("the typed input comes first, ahead of the app that takes anything",
      [r["app"] for r in view["fits"]] == ["typed", "generic"], str(view["fits"]))
check("the answer carries what to run, not just which app",
      view["fits"][0]["capability"] == "bg" and view["fits"][0]["intent"] == "remove_background")
check("whether the app is running is reported rather than assumed",
      all(r["running"] is False for r in view["fits"]))
check("an app that has never been probed is listed as unprobed, not guessed at",
      sorted(view["unprobed"]) == ["empty", "unread"], str(view["unprobed"]))
check("and it is not offered as a candidate", "empty" not in [r["app"] for r in view["fits"]])
check("a capability that wants a link is kept out of the list with its reason",
      [r["app"] for r in view["refused"]] == ["linky"]
      and "link" in view["refused"][0]["reason"], str(view["refused"]))
check("nothing is claimed to fit when nothing does",
      ac.candidates("video", [app("only", [cap(component="Image")])])["none"] is True)
check("and the file's own kind comes back with the answer",
      ac.candidates("video", [])["kind"] == "video")

order_a = ac.candidates("image", [app("zeta", [cap(component="File", score=1.0)]),
                                  app("alpha", [cap(component="File", score=1.0)])])
order_b = ac.candidates("image", [app("alpha", [cap(component="File", score=1.0)]),
                                  app("zeta", [cap(component="File", score=1.0)])])
check("two equal fits come back in the same order whichever way the apps arrive",
      [r["app"] for r in order_a["fits"]] == [r["app"] for r in order_b["fits"]]
      == ["alpha", "zeta"], str([r["app"] for r in order_a["fits"]]))

out_probe = ac.candidates("image", [
    app("plain", [cap(component="File", media_out=False)]),
    app("produces", [cap(component="File", media_out=True)]),
])
check("between two equal fits, the one that produces something comes first",
      [r["app"] for r in out_probe["fits"]] == ["produces", "plain"], str(out_probe["fits"]))

# ------------------------------------------------------------- the route
print("\n== the routing question, asked over the real machine ==")
real = PLUGIN / "README.md"
body = api.accepts(api.AcceptsBody(path=str(real)))
payload = __import__("json").loads(bytes(body.body).decode("utf-8"))
check("a real file on this machine is routed, and the file's own kind came back",
      payload.get("ok") and payload.get("kind") == "text"
      and payload.get("name") == "README.md", str({k: payload.get(k) for k in
                                                   ("ok", "kind", "name", "error")}))
check("the answer is three lists, so the panel can say what it does not know",
      all(isinstance(payload.get(k), list) for k in ("fits", "refused", "unprobed")))
check("and the installed apps were actually read", (payload.get("apps") or 0) > 0,
      str(payload.get("apps")))

started = {"called": False}


class NeverStarts:
    """A runtime that refuses to start anything, to prove the router never asks."""

    def running(self, *_a, **_k):
        return {}

    def start(self, *_a, **_k):
        started["called"] = True
        raise AssertionError("the routing question must not start an app")


real_runtime = api._runtime
api._runtime = lambda: NeverStarts()
try:
    quiet = api.accepts(api.AcceptsBody(path=str(real)))
finally:
    api._runtime = real_runtime
check("answering 'who would take this' starts nothing at all, so a right-click "
      "cannot land a model on the card", started["called"] is False
      and __import__("json").loads(bytes(quiet.body).decode("utf-8")).get("ok") is True)

missing = api.accepts(api.AcceptsBody(path=str(PLUGIN / "nope-does-not-exist.png")))
check("a path that is not there is refused as such, not routed",
      __import__("json").loads(bytes(missing.body).decode("utf-8")).get("error")
      == f"there is no file at {PLUGIN / 'nope-does-not-exist.png'}")
check("a directory is not a file, and is refused the same way",
      __import__("json").loads(bytes(api.accepts(
          api.AcceptsBody(path=str(PLUGIN))).body).decode("utf-8")).get("ok") is False)
check("and an empty path is refused before anything is looked up",
      "no file was named" in __import__("json").loads(bytes(api.accepts(
          api.AcceptsBody(path="  ")).body).decode("utf-8")).get("error", ""))

# --------------------------------------------------------- the hand-over
print("\n== handing the file over ==")
MANIFEST = {"url": "http://127.0.0.1:7999/", "kind": "gradio",
            "capabilities": [cap(name="bg", intent="remove_background")]}
calls = []


def fake_call(home, folder, capability, inputs, timeout=None, manifest=None):
    calls.append({"capability": capability, "inputs": list(inputs), "url": (manifest or {}).get("url")})
    return {"ok": True, "capability": capability, "intent": "remove_background",
            "files": [{"path": "C:/exports/out.png"}], "run_dir": "C:/runs/one",
            "log": ["uploaded the file", "the app took 2.1s"], "recipe": {"asked": capability}}


# The loader is swapped through a holder rather than rebound, so the same fake can
# answer "he has a manifest" and "he has never been probed" without a second one.
loaded: dict = {"manifest": dict(MANIFEST)}
fake = SimpleNamespace(load=lambda home, folder: (dict(loaded["manifest"])
                                                  if loaded["manifest"] else None),
                       probe=lambda home, folder, url: dict(MANIFEST, url=url),
                       call=fake_call, fits=ac.fits, candidates=ac.candidates,
                       file_kind=ac.file_kind)
real_ac, real_up, real_idle = api._appcall_module, api._bring_up, api._idle_module
api._appcall_module = lambda: fake
api._bring_up = lambda app_id, seconds=180.0: {"url": "http://127.0.0.1:7999/",
                                               "note": "was already running"}
api._idle_module = lambda: None


def run_route(app_id, capability, inputs=None, timeout=None):
    resp = api.app_call(app_id, api.CallBody(capability=capability, inputs=inputs,
                                             timeout=timeout))
    return resp.status_code, __import__("json").loads(bytes(resp.body).decode("utf-8"))


real_folder = api._app_folder
api._app_folder = (lambda app_id: PLUGIN / "dashboard" if app_id in ("pretend", "ghost")
                   else real_folder(app_id))
try:
    status, out = run_route("not-installed", "bg")
    check("an app that is not installed is a 404, and says which app", status == 404
          and "not-installed" in str(out.get("error")), str(out))

    loaded["manifest"] = None
    ok_status, out = run_route("ghost", "bg")
    check("an app that was never probed gets a sentence about that, not a job",
          ok_status == 200 and not out.get("ok")
          and "no list of what this app can do" in str(out.get("error")), str(out))
    check("and nothing was started or called to find that out",
          calls == [], str(calls))
    loaded["manifest"] = dict(MANIFEST)

    status, out = run_route("pretend", "not_a_capability")
    check("a capability that is not in the manifest is refused, and the ones that "
          "are get listed", not out.get("ok") and out.get("available") == ["remove_background"],
          str(out))
    check("no job was made for either refusal", calls == [])

    status, out = run_route("pretend", "remove_background")
    check("a capability whose required input was not passed is refused with what "
          "it needs", not out.get("ok") and out.get("inputs")
          and out["inputs"][0]["name"] == "image"
          and out["inputs"][0]["takes_a_local_file"] is True, str(out))
    check("still no job, and the app was never called", calls == [])

    status, out = run_route("pretend", "remove_background", inputs=[str(real)])
    check("a proper call becomes a job the panel can watch",
          out.get("ok") and str(out.get("job", "")).startswith("call-")
          and out.get("capability") == "bg", str(out))

    jid = str(out.get("job"))
    import time as _t
    deadline = _t.time() + 15
    while _t.time() < deadline:
        job = api._JOBS.get(jid) or {}
        if job.get("state") in ("done", "failed"):
            break
        _t.sleep(0.2)
    job = api._JOBS.get(jid) or {}
    check("and it ends on its own with the files the app produced",
          job.get("state") == "done"
          and (job.get("result") or {}).get("files") == ["C:/exports/out.png"],
          str(job.get("result") or job.get("error")))
    check("the file was passed through as the caller wrote it",
          calls and calls[-1]["inputs"] == [str(real)] and calls[-1]["capability"] == "bg",
          str(calls))
    log = " | ".join(job.get("log") or [])
    check("what the app said is kept in the job's log, so the panel has something "
          "to show while it works", "the app took 2.1s" in log, log[:200])
    check("and the artifact is named in the log as it lands",
          "wrote C:/exports/out.png" in log, log[:200])
    check("the url the manifest held was the one the call was made against",
          calls[-1]["url"] == "http://127.0.0.1:7999/", str(calls[-1]))
finally:
    api._appcall_module, api._bring_up, api._idle_module = real_ac, real_up, real_idle
    api._app_folder = real_folder

print(f"\nt_accepts: {PASS} passed, {FAIL} failed")
if FAIL:
    for f in FAILURES:
        print("   failing: " + f)
sys.exit(1 if FAIL else 0)
