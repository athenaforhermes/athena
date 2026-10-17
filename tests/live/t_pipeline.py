"""A chain of apps: one's output handed to the next one's input.

A pipeline is where a tool that routes one file stops being enough — cut the
background out, then upscale what comes back, then wrap it as a file the next app
will take. The interesting parts are all about not lying:

  * the handover is checked between the two manifests *before* anything runs, so a
    chain that cannot work is refused instead of discovered halfway through with a
    job already going;
  * what cannot be known up front is said out loud instead of promised — an app
    that does not declare what it produces may still be chained, and the log says
    the handover was not certain;
  * a stage that produces no files stops the chain and says so, because silence
    would leave a step that looks like it ran and did nothing;
  * a stage that fails ends the job failed with that stage named, and the files
    that did come out are still reported — they are on disk either way;
  * the whole chain is one job, because a person watching it wants one bar.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# Derived (with an override) rather than written down as one machine's directory: the suite
# ships in the repo, so it has to run somewhere other than where it was written.
PLUGIN = Path(os.environ.get("ATHENA_PLUGIN")
              or Path(__file__).resolve().parent.parent.parent)

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
    print("t_pipeline: 0 passed, 1 failed")
    sys.exit(1)


def cap(name="do_it", intent="do_this", component="Image", path=True, media=True,
        required=True, multi=False, outputs=(("Image", True),), score=9.0, inputs=None):
    """One capability in the shape the probe writes them."""
    if inputs is None:
        inputs = [{"name": "image", "component": component, "accepts_local_path": path,
                   "media": media, "required": required, "multi": multi}]
    rows = [{"label": f"out{n}", "type": "pil", "component": comp} for n, (comp, _m) in
            enumerate(outputs) if comp]
    return {"name": name, "intent": intent, "inputs": inputs, "media_in": media,
            "media_out": any(m for _c, m in outputs), "outputs": rows,
            "accepts_local_files": any(i["accepts_local_path"] for i in inputs),
            "score": score}


# ------------------------------------------------------- what the handover says
print("\n== whether one app's output is the next app's input ==")
verdict = ac.links(cap(outputs=(("Image", True),)), cap(component="Image"))
check("an image out and an image in is a certain handover",
      verdict["ok"] and verdict["certain"] and verdict["kinds"] == ["image"]
      and verdict["reason"] == "it takes an image", str(verdict))

verdict = ac.links(cap(outputs=(("Image", True),)), cap(component="File"))
check("and an app that takes any file takes it too, said in its own words",
      verdict["ok"] and verdict["certain"] and verdict["reason"] == "the next one takes any file",
      str(verdict))

verdict = ac.links(cap(outputs=(("Image", True),)), cap(component="Video"))
check("an image out and a video in is not a handover, and the refusal names both "
      "sides in the words a person uses",
      not verdict["ok"] and verdict["reason"] == "it produces an image and the next one takes a video",
      str(verdict))

verdict = ac.links(cap(outputs=(("Video", True), ("Image", True))), cap(component="Video"))
check("one matching output among several is enough — the app can be fed",
      verdict["ok"] and verdict["kinds"] == ["video", "image"], str(verdict))

verdict = ac.links(cap(outputs=(("Image", True),)), cap(component="Textbox", path=False,
                                                        media=False))
check("an app that takes no file at all cannot follow one that makes them",
      not verdict["ok"] and "does not take a file" in verdict["reason"], str(verdict))

verdict = ac.links(cap(outputs=(("File", True),)), cap(component="Image"))
check("an app that does not type what it produces may run, but the handover is "
      "not promised", verdict["ok"] and verdict["certain"] is False, str(verdict))
check("and it says which of the two happened, rather than staying silent",
      verdict["reason"] == "it does not say what it produces", str(verdict))

verdict = ac.links(cap(outputs=()), cap(component="Image"))
check("a capability with no declared outputs is the same case", verdict["ok"]
      and verdict["certain"] is False, str(verdict))


# ------------------------------------------------------------------- the fixture
print("\n== the fixture ==")
CUT = cap(name="cut", intent="remove_background", outputs=(("Image", True),))
UP = cap(name="up", intent="upscale", outputs=(("Image", True),))
WRAP = cap(name="wrap", intent="pack", component="Files", multi=True,
           outputs=(("File", True),))
NEEDS_TWO = cap(name="plate", intent="compose", component="Image",
                inputs=[{"name": "prompt", "component": "Textbox", "accepts_local_path": False,
                         "media": False, "required": True, "multi": False},
                        {"name": "image", "component": "Image", "accepts_local_path": True,
                         "media": True, "required": True, "multi": False}],
                outputs=(("Image", True),))
VID = cap(name="t2v", intent="text_to_video", component="Video",
          inputs=[{"name": "video", "component": "Video", "accepts_local_path": True,
                   "media": True, "required": True, "multi": False},
                  {"name": "prompt", "component": "Textbox", "accepts_local_path": False,
                   "media": False, "required": False, "multi": False}],
          outputs=(("Video", True),))

FOLDERS = {"pretend": "a", "second": "b", "third": "c", "wanish": "v", "plate": "p"}
MANIFESTS = {
    "pretend": {"capabilities": [CUT]},
    "second": {"capabilities": [UP]},
    "third": {"capabilities": [WRAP]},
    "wanish": {"capabilities": [VID]},
    "plate": {"capabilities": [NEEDS_TWO]},
}
calls: list = []
# What each capability hands back, stage by stage: the point of the fixture is that
# the files move between the calls, not that a fake answered.
BEHAVIOUR = {
    "cut": {"ok": True, "files": ["C:/runs/one/cut.png"]},
    "up": {"ok": True, "files": ["C:/runs/two/up.png"]},
    "wrap": {"ok": True, "files": ["C:/runs/three/pack.zip"]},
}


def fake_call(home, folder, capability, inputs, timeout=None, manifest=None):
    calls.append({"capability": capability, "inputs": list(inputs)})
    behaviour = BEHAVIOUR.get(capability)
    if behaviour is None:
        return {"ok": False, "capability": capability, "error": "no such behaviour"}
    if behaviour.get("raises"):
        return {"ok": False, "capability": capability, "error": behaviour["raises"]}
    if "files" not in behaviour:
        return {"ok": True, "capability": capability, "intent": "did_it",
                "files": [], "run_dir": "C:/runs/one", "log": ["nothing came back"]}
    return {"ok": True, "capability": capability, "intent": "did_it",
            "files": [{"path": f} for f in behaviour["files"]],
            "run_dir": "C:/runs/one", "log": ["the app took 2.1s"]}


folder_name = {"v": "wanish", "p": "plate", "a": "pretend", "b": "second", "c": "third"}
fake = SimpleNamespace(
    load=lambda home, folder: dict(MANIFESTS.get(folder_name.get(Path(folder).name, "a")) or {}),
    probe=lambda home, folder, url: dict(MANIFESTS.get(folder_name.get(Path(folder).name, "a")) or {},
                                         url=url),
    call=fake_call, fits=ac.fits, candidates=ac.candidates, links=ac.links,
    file_kind=ac.file_kind)

real_ac, real_up, real_idle, real_folder = (api._appcall_module, api._bring_up,
                                            api._idle_module, api._app_folder)
api._appcall_module = lambda: fake
api._bring_up = lambda app_id, seconds=180.0: {"url": f"http://127.0.0.1:79{len(app_id) % 9}/",
                                               "note": "was already running"}
api._idle_module = lambda: None
api._app_folder = (lambda app_id: PLUGIN / "dashboard" / FOLDERS.get(app_id, "nope")
                   if app_id in FOLDERS else real_folder(app_id))


def run_route(app_id, capability, inputs=None, then=None, timeout=None):
    body = api.CallBody(capability=capability, inputs=inputs, timeout=timeout, then=then)
    resp = api.app_call(app_id, body)
    return resp.status_code, json.loads(bytes(resp.body).decode("utf-8"))


def wait(jid, seconds=20.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        job = api._JOBS.get(jid) or {}
        if job.get("state") in ("done", "failed"):
            return job
        time.sleep(0.2)
    return api._JOBS.get(jid) or {}


real = PLUGIN / "README.md"
try:
    # ------------------------------------------------- refusals before any job
    print("\n== what is refused before the chain starts ==")
    status, out = run_route("pretend", "cut", inputs=[str(real)],
                            then=[{"app": "second", "capability": "up"},
                                  {"app": "ghost", "capability": "up"}])
    check("a stage naming an app that is not installed is refused, and the stage "
          "is named by number", not out.get("ok") and "stage 3" in str(out.get("error"))
          and "ghost" in str(out.get("error")), str(out))

    status, out = run_route("pretend", "cut", inputs=[str(real)], then=[{"app": "second"}])
    check("a stage that does not name a capability is refused before anything runs",
          not out.get("ok") and "stage 2" in str(out.get("error"))
          and "does not name a capability" in str(out.get("error")), str(out))

    status, out = run_route("pretend", "cut", inputs=[str(real)],
                            then=[{"app": "second", "capability": "not_a_thing"}])
    check("a stage whose capability is not in that app's manifest is refused, and "
          "what it does have is named",
          not out.get("ok") and "no capability called" in str(out.get("error"))
          and "upscale" in str(out.get("error")), str(out))

    status, out = run_route("pretend", "cut", inputs=[str(real)],
                            then=[{"app": "wanish", "capability": "t2v"}])
    check("a handover the two manifests do not support is refused as a mismatch",
          not out.get("ok") and out.get("produces") == ["image"]
          and out.get("takes") == ["video"], str(out))
    check("and the sentence says which side is which, so the person can fix it",
          "it produces an image and the next one takes a video" in str(out.get("error")),
          str(out.get("error")))
    check("the refusal is a 400, because nothing about the request can run",
          status == 400, str(status))

    status, out = run_route("pretend", "cut", inputs=[str(real)],
                            then=[{"app": "plate", "capability": "compose"}])
    check("a stage that needs more inputs than the files can fill is refused, and "
          "told how to give them", not out.get("ok")
          and "it needs 2 input(s)" in str(out.get("error"))
          and "{files}" in str(out.get("error")), str(out))

    check("and none of those refusals started an app, called one, or made a job",
          calls == [], str(calls))
    check("no job was created either", not [j for j in api._JOBS.values()
                                            if j.get("kind") == "call"], "a job exists")

    # ------------------------------------------------------- the chain runs
    print("\n== the chain, run ==")
    status, out = run_route("pretend", "cut", inputs=[str(real)],
                            then=[{"app": "second", "capability": "up"}])
    check("a chain that holds up becomes one job, and the panel is told how many "
          "stages follow", out.get("ok") and out.get("stages") == ["second"], str(out))
    job = wait(str(out.get("job")))
    result = job.get("result") or {}
    check("it ends on its own, done, with the last stage's files",
          job.get("state") == "done" and result.get("files") == ["C:/runs/two/up.png"],
          str(result or job.get("error")))
    check("the files the first app made were the inputs the second one was given",
          len(calls) == 2 and calls[1]["inputs"] == ["C:/runs/one/cut.png"], str(calls))
    check("and the second app ran its own capability, not the first one's",
          calls[1]["capability"] == "up", str(calls))
    check("each stage is reported, in order, with what it made",
          [s.get("stage") for s in (result.get("stages") or [])] == [1, 2]
          and (result["stages"][1].get("files") == ["C:/runs/two/up.png"]), str(result.get("stages")))
    log = " | ".join(job.get("log") or [])
    check("the log names the stage as it starts, and how many files were handed over",
          "--- stage 2: second" in log and "1 file(s) handed over" in log, log[:300])
    check("and every artifact is still named as it lands",
          "wrote C:/runs/one/cut.png" in log and "wrote C:/runs/two/up.png" in log, log[:300])

    # ------------------------------------------------------- three stages
    calls.clear()
    status, out = run_route("pretend", "cut", inputs=[str(real)],
                            then=[{"app": "second", "capability": "up"},
                                  {"app": "third", "capability": "wrap"}])
    job = wait(str(out.get("job")))
    result = job.get("result") or {}
    check("a three-stage chain carries the files through, each stage feeding the "
          "next and not the first", job.get("state") == "done"
          and calls[2]["inputs"] == ["C:/runs/two/up.png"]
          and result.get("files") == ["C:/runs/three/pack.zip"], str(calls) + str(result))
    check("and every stage is in the report", len(result.get("stages") or []) == 3,
          str(result.get("stages")))

    # ------------------------------------------- a stage with other arguments
    calls.clear()
    status, out = run_route("pretend", "cut", inputs=[str(real)],
                            then=[{"app": "plate", "capability": "compose",
                                   "inputs": ["a red bicycle", "{file}"]}])
    job = wait(str(out.get("job")))
    check("a stage may name its other arguments, with {file} where the handover goes",
          job.get("state") == "done"
          and calls[1]["inputs"] == ["a red bicycle", "C:/runs/one/cut.png"], str(calls))

    # ------------------------------------------------- a stage that makes nothing
    calls.clear()
    BEHAVIOUR["cut"] = {"ok": True, "files": []}
    status, out = run_route("pretend", "cut", inputs=[str(real)],
                            then=[{"app": "second", "capability": "up"}])
    job = wait(str(out.get("job")))
    result = job.get("result") or {}
    # The job itself ends "done" as every job does — the answer it carries is the
    # one that says the chain stopped short, and that is what a watcher reads.
    check("a stage that produces no files stops the chain, and says so — silence "
          "would look like a step that ran and did nothing",
          job.get("state") == "done" and result.get("ok") is False
          and result.get("stopped_at") == 2
          and "stage 1 produced no files, so stage 2 was not started" in str(
              result.get("error") or ""), str(result))
    check("and the next app was never called", len(calls) == 1, str(calls))

    # ------------------------------------------------------- a stage that fails
    calls.clear()
    BEHAVIOUR["cut"] = {"ok": True, "files": ["C:/runs/one/cut.png"]}
    BEHAVIOUR["up"] = {"raises": "the app ran out of memory"}
    status, out = run_route("pretend", "cut", inputs=[str(real)],
                            then=[{"app": "second", "capability": "up"}])
    job = wait(str(out.get("job")))
    result = job.get("result") or {}
    check("a stage that fails stops the chain, with the stage named",
          job.get("state") == "done" and result.get("ok") is False
          and result.get("stopped_at") == 2
          and "stage 2 (second)" in str(result.get("error")), str(result))
    check("the files that did come out are still reported — they are on disk anyway",
          result.get("files") == [], str(result.get("files")))
    log = " | ".join(job.get("log") or [])
    check("and the log says the chain stopped rather than trailing off",
          "stage 2 (second) failed, so the chain stopped" in log, log[:300])
finally:
    api._appcall_module, api._bring_up, api._idle_module = real_ac, real_up, real_idle
    api._app_folder = real_folder

print(f"\nt_pipeline: {PASS} passed, {FAIL} failed")
if FAIL:
    for f in FAILURES:
        print("   failing: " + f)
sys.exit(1 if FAIL else 0)
