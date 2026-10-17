"""Pulling weights: a repository by name, into a folder an app actually reads.

The download itself is not this plugin's work — `hf` already resumes a partial
file and hashes what it fetched — and this test does not pretend to have proved
it. What it proves is everything wrapped around that: that a repository is
described before it is fetched (size, files, whether a copy is already here), that
the destination is resolved to a library an app reads rather than a path someone
typed, that the download is a job with a real byte count, that two downloads
never run at once, and that a failure says what failed.

The child process is a stand-in for `hf` (`pull_stub.cmd` → `pull_stub.py`), for
the same reason the engine is stubbed elsewhere: a test that needs the network is
a test that fails for reasons that are not the plugin. The real CLI was exercised
against HuggingFace directly — a genuine 443 KB weight fetched into a real
library's `checkpoints/`, progress counted from the folder, and a second pull
into the same folder writing **0 bytes** because the file was already there.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
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

PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")
HERE = Path(__file__).resolve().parent
STUB = HERE / "pull_stub.cmd"

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

# Everything this test does is inside one scratch home: `_athena_home` is what
# decides where apps are found, where the shared cache is, and where the `hf`
# command is looked for, so pointing it here keeps the real machine untouched.
home = Path(tempfile.mkdtemp(prefix="athena-pull-"))
api._athena_home = lambda: home
(home / "api").mkdir(parents=True)


def make_app(name: str) -> Path:
    d = home / "api" / name
    (d / "app").mkdir(parents=True, exist_ok=True)
    return d


def make_library(path: Path, checkpoints: int = 1) -> Path:
    (path / "checkpoints").mkdir(parents=True, exist_ok=True)
    for i in range(checkpoints):
        (path / "checkpoints" / f"c{i}.safetensors").write_bytes(b"x" * 32)
    return path


def point_at(app: Path, library: Path) -> None:
    (app / "app" / "extra_model_paths.yaml").write_text(
        "athena_shared:\n"
        f"    base_path: {library.as_posix()}\n"
        "    checkpoints: checkpoints\n", encoding="utf-8")


print("== which files a pattern selects, because the size has to be real ==")
FILES = [{"file": "unet/model.safetensors", "bytes": 100},
         {"file": "model.safetensors", "bytes": 200},
         {"file": "config.json", "bytes": 10},
         {"file": "vae/vae.safetensors", "bytes": 50}]
check("no pattern takes the whole repository",
      len(api._pull_matches(FILES, None)) == 4, "")
check("a bare name takes exactly that file",
      [f["file"] for f in api._pull_matches(FILES, "model.safetensors")]
      == ["unet/model.safetensors", "model.safetensors"], "")
check("a glob takes every match, including one inside a folder",
      sorted(f["file"] for f in api._pull_matches(FILES, "*.safetensors"))
      == ["model.safetensors", "unet/model.safetensors", "vae/vae.safetensors"], "")
check("a folder pattern takes what is under it",
      [f["file"] for f in api._pull_matches(FILES, "unet/*")] == ["unet/model.safetensors"], "")
check("comma-separated patterns are all of them, not a literal",
      sorted(f["file"] for f in api._pull_matches(FILES, "config.json, vae/*"))
      == ["config.json", "vae/vae.safetensors"], "")

print("\n== where a pull lands, which is what makes it usable ==")
shared = make_library(home / "weights", checkpoints=2)
pointed = make_app("A.git")
point_at(pointed, shared)
body = api.PullBody(repo="owner/thing", app="A")
d = api._pull_dest(home, body)
check("an app already pointed at a shared library gets the file filed there",
      d["ok"] and d["dest"] == shared / "checkpoints", json.dumps(d, default=str))
check("and the answer says why, so the path is not a mystery",
      "A" in (d.get("because") or ""), str(d.get("because")))

lone = make_app("B.git")
d2 = api._pull_dest(home, api.PullBody(repo="owner/thing", app="B"))
check("an app with no mapping of its own is filed where ComfyUI reads by default",
      d2["ok"] and d2["dest"] == lone / "app" / "models" / "checkpoints",
      json.dumps(d2, default=str))
d3 = api._pull_dest(home, api.PullBody(repo="owner/thing", folder=(shared / "checkpoints").as_posix()))
check("a folder named as a leaf is folded back to the library that holds it",
      d3["ok"] and d3["dest"] == shared / "checkpoints", json.dumps(d3, default=str))
d4 = api._pull_dest(home, api.PullBody(repo="owner/thing", app="A", key="loras"))
check("a LoRA can be filed under loras instead",
      d4["ok"] and d4["dest"] == shared / "loras", json.dumps(d4, default=str))
d5 = api._pull_dest(home, api.PullBody(repo="owner/thing", app="A", key="nonsense"))
check("a folder ComfyUI does not read is refused rather than created",
      d5["ok"] is False and "nonsense" in d5.get("error", ""), str(d5.get("error")))
d6 = api._pull_dest(home, api.PullBody(repo="owner/thing", app="ghost"))
check("an app that is not installed is refused by name",
      d6["ok"] is False and "ghost" in d6.get("error", ""), str(d6.get("error")))
d7 = api._pull_dest(home, api.PullBody(repo="owner/thing"))
check("with nothing named it is a cache entry, and says so rather than guessing",
      d7["ok"] and d7["dest"] is None and d7["cache_only"], json.dumps(d7, default=str))

print("\n== a repository is described before it is fetched ==")
api._hf_http_json = lambda url, token="": {"ok": True, "data": {
    "id": "owner/thing", "private": False, "gated": False,
    "siblings": [{"rfilename": "config.json", "size": 10},
                 {"rfilename": "small.safetensors", "size": 100},
                 {"rfilename": "big.safetensors", "size": 5000}]}}
r = api.hf_remote("owner/thing", home)
check("the files come back largest first, because that is the decision",
      [f["file"] for f in r["files"]] == ["big.safetensors", "small.safetensors", "config.json"],
      str([f["file"] for f in r["files"]]))
check("the weights are named apart from everything else",
      [f["file"] for f in r["weights"]] == ["big.safetensors", "small.safetensors"],
      str(r["weights"]))
check("the size is the whole repository, and a sentence says it too",
      r["bytes"] == 5110 and "5 KB" in r["summary"], r["summary"])
check("nothing cached yet, so a download is a real download",
      r["already_cached"] is False, json.dumps(r["already_cached"]))

cached = home / "cache" / "HF_HOME" / "hub" / "models--owner--thing" / "snapshots" / "rev"
cached.mkdir(parents=True, exist_ok=True)
(cached / "big.safetensors").write_bytes(b"y" * 5000)
r2 = api.hf_remote("owner/thing", home)
check("a repository already on this disk is named as already here",
      r2["already_cached"] and "already cached" in r2["summary"], r2["summary"])
check("and the copy is what makes pulling it again cheap, so it is stated",
      "local cache" in (r2.get("cached_note") or ""), str(r2.get("cached_note")))
no_weights = {"ok": True, "data": {"id": "owner/configs", "siblings": [
    {"rfilename": "config.json", "size": 10}, {"rfilename": "tokenizer.json", "size": 20}]}}
api._hf_http_json = lambda url, token="": no_weights
r3 = api.hf_remote("owner/configs", home)
check("a repository of configs is not passed off as a model to download",
      r3["has_generating_weights"] is False and "no weights in it" in r3["summary"],
      r3["summary"])
check("a name that is not `owner/name` is refused before any request",
      api.hf_remote("justaname", home).get("ok") is False, "")

print("\n== the download is a job, and two of them never run at once ==")
api._hf_cli = lambda h: STUB
journal = home / "pull-journal.txt"
os.environ["ATH_STUB_JOURNAL"] = journal.as_posix()
os.environ["ATH_STUB_BYTES"] = "4096"
# Long enough that the second request genuinely arrives mid-download: the queue
# label only exists while something is actually being fetched, and a test that
# races a two-second child proves nothing either way.
os.environ["ATH_STUB_SLEEP"] = "6"
os.environ.pop("ATH_STUB_FAIL", None)

FAKE = {"ok": True, "repo": "owner/thing", "private": False, "gated": False,
        "files": [{"file": "model.safetensors", "bytes": 4096}], "count": 1,
        "bytes": 4096, "weights": [{"file": "model.safetensors", "bytes": 4096}],
        "weights_bytes": 4096, "has_generating_weights": True, "already_cached": False,
        "cached_folder": None, "cached_files": [], "cached_note": None,
        "summary": "owner/thing: 1 files, 4 KB, of which 1 are weights (4 KB)"}
api.hf_remote = lambda repo, home=None: dict(FAKE)

one = make_app("C.git")
two = make_app("D.git")
r1 = json.loads(api.models_download(api.PullBody(repo="owner/thing", app="C")).body.decode())
check("starting a pull answers with the job to watch, not with a model",
      r1["ok"] and r1["job"].startswith("download-") and r1["watch"] == f"/jobs/{r1['job']}",
      json.dumps(r1)[:180])
check("and with what it will cost before it starts",
      r1["bytes"] == 4096 and r1["files"] == 1 and r1["dest"].endswith("models/checkpoints"),
      json.dumps(r1)[:200])
st1 = json.loads(api.job_status(r1["job"]).body.decode())
first_state = st1["state"]
check("the first pull is running while the second is asked for, which is the only "
      "state in which a queue can be observed",
      first_state == "running", first_state)
# The two ages a person watching an install actually uses. A panel showing
# "working…" over a hung job is the bug these exist to kill, so both have to be
# real numbers while the job is still going — not None, which is what a missing
# stamp would look like.
check("a running job says how long it has been going",
      isinstance(st1.get("elapsed"), (int, float)) and st1["elapsed"] >= 0,
      str(st1.get("elapsed")))
check("and how long since it last said anything",
      isinstance(st1.get("since_output"), (int, float)) and st1["since_output"] >= 0,
      str(st1.get("since_output")))
check("and when the bar last moved, which counts as saying something too",
      isinstance(st1.get("since_move"), (int, float)) and st1["since_move"] >= 0,
      str(st1.get("since_move")))
check("the raw stamps are still there for anyone reading the record",
      isinstance(st1.get("started"), (int, float)) and st1.get("ended") is None,
      f"started={st1.get('started')} ended={st1.get('ended')}")
r2 = json.loads(api.models_download(api.PullBody(repo="owner/thing", app="D")).body.decode())
first_label = (json.loads(api.job_status(r2["job"]).body.decode()).get("progress") or {}).get("label", "")
check("a second pull queues behind the first instead of splitting the link",
      "queued" in first_label, f"label was {first_label!r}")

deadline = time.time() + 120
states = {}
while time.time() < deadline:
    states = {j: json.loads(api.job_status(j).body.decode())["state"]
              for j in (r1["job"], r2["job"])}
    if all(s != "running" for s in states.values()):
        break
    time.sleep(1)
check("both pulls finish", all(s == "done" for s in states.values()), json.dumps(states))
st_done = json.loads(api.job_status(r1["job"]).body.decode())
check("a finished job's age is the whole run, not a clock still counting",
      isinstance(st_done.get("elapsed"), (int, float)) and st_done["elapsed"] >= 0
      and isinstance(st_done.get("ended"), (int, float)),
      f"elapsed={st_done.get('elapsed')} ended={st_done.get('ended')}")
check("a running job's price is frozen once it ends, so the row stops moving",
      st_done["elapsed"] == st1["elapsed"] or st_done["elapsed"] >= st1["elapsed"],
      f"{st1.get('elapsed')} -> {st_done.get('elapsed')}")
res1 = json.loads(api.job_status(r1["job"]).body.decode())["result"]
check("the file is in the folder the app reads",
      (one / "app" / "models" / "checkpoints" / "model.safetensors").stat().st_size == 4096,
      str(res1))
check("and the job reports the bytes it actually wrote",
      res1["bytes_written"] == 4096 and res1["weights"] == 1 and res1["files"] == 1,
      json.dumps(res1))
# `hf` leaves its own record of what it copied inside the destination; a file
# count that included it would report files nobody asked for. Injected here
# because the stand-in CLI does not write that folder itself.
(home / "api" / "C.git" / "app" / "models" / "checkpoints" / ".cache" / "huggingface").mkdir(
    parents=True, exist_ok=True)
(home / "api" / "C.git" / "app" / "models" / "checkpoints" / ".cache" / "huggingface"
 / "CACHEDIR.TAG").write_bytes(b"0")
os.environ["ATH_STUB_SLEEP"] = "0"
r4 = json.loads(api.models_download(api.PullBody(repo="owner/thing", app="C")).body.decode())
while True:
    st4 = json.loads(api.job_status(r4["job"]).body.decode())
    if st4["state"] != "running":
        break
    time.sleep(1)
res4 = st4["result"]
check("hf's own bookkeeping inside the destination is not counted as a download",
      res4["files"] == 1 and res4["weights"] == 1, json.dumps(res4))
check("but it is named, so a dot-folder in a weights library is explained",
      "record of what it has already copied" in (res4.get("local_metadata") or ""),
      str(res4.get("local_metadata"))[:120])

# Only the first two pulls: this journal is shared with the later one, and the
# question here is whether *these* two overlapped.
spans = []
for line in journal.read_text(encoding="utf-8").splitlines()[:4]:
    kind, t, pid = line.split()
    spans.append((kind, float(t), pid))
starts = [s for s in spans if s[0] == "start"]
ends = [s for s in spans if s[0] == "end"]
check("the queue ran both, oldest first",
      len(starts) == 2 and len(ends) == 2, json.dumps(spans))
check("and never let the two overlap — one link, one download",
      starts[1][1] >= ends[0][1], json.dumps(spans))

os.environ["ATH_STUB_FAIL"] = "gated: ask for access"
r3 = json.loads(api.models_download(api.PullBody(repo="owner/thing", app="C")).body.decode())
while True:
    st = json.loads(api.job_status(r3["job"]).body.decode())
    if st["state"] != "running":
        break
    time.sleep(1)
check("a pull that fails is a failed job, not a silent success",
      st["state"] == "failed" and "exited 1" in (st.get("error") or ""), json.dumps(st)[:220])
check("and the failure carries the reason the CLI gave",
      "gated" in json.dumps(st), json.dumps(st)[:220])
check("with the hint that says what to do about it",
      "token" in ((st.get("result") or {}).get("hint") or ""), json.dumps(st.get("result"))[:200])
os.environ.pop("ATH_STUB_FAIL", None)

api._hf_cli = lambda h: None
no_cli = api.models_download(api.PullBody(repo="owner/thing", app="C"))
check("with no `hf` command to run, the refusal names that rather than failing later",
      no_cli.status_code == 503 and "hf" in json.loads(no_cli.body.decode())["error"],
      no_cli.body.decode()[:200])

print()
if FAIL:
    print(f"{PASS}/{PASS + FAIL} checks passed — FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print(f"{PASS}/{PASS + FAIL} checks passed")
