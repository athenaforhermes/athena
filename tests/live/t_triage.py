"""Naming why an app stopped, against the strings this machine actually produced.

A triage reader is only worth having if its patterns match reality, and reality is
the hard part: a pattern written from memory matches the message somebody
remembers, not the one PyTorch prints. So every fixture below is a string that was
captured on this box, or read out of the source of the program that prints it, and
the comment above each one says where to get it again:

  * the CUDA OOM — printed by the RTX 5090 through ComfyUI's own interpreter
    (`api/comfy.git/app/env/Scripts/python.exe`) asking for a 120 GB tensor.
  * the port clash — two `bind()` calls on one port, on Windows.
  * the missing weight — ComfyUI's own sentence, from `folder_paths.py`'s
    `get_full_path_or_raise`.
  * the gated repository — HuggingFace's real 401 body for a gated file.
  * the missing module — Python itself.

Two tests here exist because of defects found while writing this, and they are
named as such: a CUDA OOM used to be read as a *system* memory failure, because
`MemoryError` is a substring of PyTorch's `OutOfMemoryError`.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
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


tri = load("athena_triage_under_test", PLUGIN / "dashboard" / "triage.py")
api = load("athena_api_under_test", PLUGIN / "dashboard" / "plugin_api.py")

# ------------------------------------------------------------------ the strings

# Real, captured. `Tried to allocate 111.76 GiB` is the probe's own numbers.
CUDA_OOM = (
    "Traceback (most recent call last):\n"
    '  File "oom_probe.py", line 5, in <module>\n'
    '    x = torch.empty(int(3e10), dtype=torch.float32, device="cuda")\n'
    "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 111.76 GiB. "
    "GPU 0 has a total capacity of 31.82 GiB of which 30.26 GiB is free. Of the "
    "allocated memory 0 bytes is allocated by PyTorch, and 0 bytes is reserved by "
    "PyTorch but unallocated. If reserved but unallocated memory is large try "
    "setting PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True to avoid "
    "fragmentation.\n"
)

# The same real sentence with the card mostly *occupied* instead of mostly free.
# The shape is real and the parse is the same one the test above holds down; only
# the free-memory figure is changed, because this machine was free when the real
# one was captured and a full card cannot be staged on demand.
CUDA_OOM_BUSY = CUDA_OOM.replace("of which 30.26 GiB is free", "of which 0.90 GiB is free")

# Real: OSError from a second bind() on a port already listening (Windows).
PORT_WIN = (
    "Traceback (most recent call last):\n"
    '  File "app.py", line 21, in <module>\n'
    "OSError: [WinError 10048] Only one usage of each socket address "
    "(protocol/network address/port) is normally permitted\n"
)
# The same failure on Linux, which words it differently.
PORT_NIX = "OSError: [Errno 98] Address already in use\n"

# Real: ComfyUI's own sentence (folder_paths.get_full_path_or_raise).
MISSING_WEIGHT = (
    "Error occurred when executing KSampler:\n"
    "FileNotFoundError: Model in folder 'checkpoints' with filename "
    "'sd_xl_base_1.0.safetensors' not found.\n"
)

# Real: HuggingFace's 401 body for a gated file.
GATED = (
    "huggingface_hub.errors.GatedRepoError: 401 Client Error. (Request ID: abc)\n"
    "Cannot access gated repo for url "
    "https://huggingface.co/meta-llama/Llama-2-7b/resolve/main/config.json.\n"
    "Access to model meta-llama/Llama-2-7b is restricted. You must have access to "
    "it and be authenticated to access it. Please log in.\n"
)

# Real: Python, on `import zz_nope_nothing`.
NO_MODULE = "ModuleNotFoundError: No module named 'insightface'\n"

# Real: a genuine interpreter-level MemoryError, which *is* a system-memory
# failure — the case that must not be confused with the CUDA one above.
REAL_MEMORY_ERROR = "Traceback (most recent call last):\nMemoryError\n"

print("== the graphics card, read properly (and not as something else) ==")
r = tri.triage(CUDA_OOM, {"app": "comfy.git"})
check("a real CUDA OOM is read as the card", r["cause"] == "cuda_oom", str(r["cause"]))
check("and it is not read as a system-memory failure — MemoryError is a substring "
      "of OutOfMemoryError, which is how this was wrong once",
      r["cause"] != "out_of_ram" and "out_of_ram" not in [a["cause"] for a in r["also"]],
      str([a["cause"] for a in r["also"]]))
check("the numbers in the real sentence are quoted back",
      "111.76 GiB" in r["why"] and "31.82 GiB" in r["why"], r["why"])
check("a card that was mostly free makes it the request that was too big, "
      "not a neighbour to close",
      r["fix"]["action"] == "none" and "larger" in r["why"], str(r["fix"]))
check("the evidence is the matched line, with its number",
      r["evidence"]["line"] == 4 and "OutOfMemoryError" in r["evidence"]["text"],
      str(r["evidence"]))

print("\n== the same sentence, on a card something else is holding ==")
r = tri.triage(CUDA_OOM_BUSY, {"app": "comfy.git", "running": ["comfy.git", "wan.git"]})
check("a mostly-occupied card is read as a card being held",
      r["fix"]["action"] == "stop_app" and r["fix"]["apps"] == ["wan.git"], str(r["fix"]))
check("and the neighbour is named in the sentence, not left to be guessed",
      "wan.git" in r["why"], r["why"])
check("the app itself is never offered as the thing to stop",
      "comfy.git" not in r["fix"]["apps"], str(r["fix"]["apps"]))
# Both answers are right and they cost different things — closing the neighbour
# throws its work away, waiting does not — so the reading offers both and lets the
# person choose, rather than picking one and calling it the fix.
check("a held card also offers waiting, because both answers are true",
      (r["fix"].get("alt") or {}).get("action") == "wait", str(r["fix"].get("alt")))
check("and the waiting answer names what it is waiting for",
      "wan.git" in ((r["fix"].get("alt") or {}).get("detail") or ""),
      str(r["fix"].get("alt")))

print("\n== a portable, and a real, socket clash ==")
r = tri.triage(PORT_WIN, {"app": "wan.git"})
check("Windows' WinError 10048 is read as a port already taken",
      r["cause"] == "port_busy", str(r["cause"]))
check("and the action is to stop the app that owns it",
      r["fix"]["action"] == "stop_app" and r["fix"]["apps"] == ["wan.git"], str(r["fix"]))
check("Linux's wording matches too",
      tri.triage(PORT_NIX)["cause"] == "port_busy")

print("\n== a weight that is not where the app looks ==")
r = tri.triage(MISSING_WEIGHT)
check("ComfyUI's own sentence is read as a missing weight",
      r["cause"] == "missing_weights", str(r["cause"]))
check("the file and the folder are parsed out of it",
      r["fix"]["file"] == "sd_xl_base_1.0.safetensors" and r["fix"]["folder"] == "checkpoints",
      str(r["fix"]))
check("the action offers the folder this machine proved it has",
      r["fix"]["action"] == "point_models", str(r["fix"]))
check("and it says this is not breakage, because it is not",
      "not breakage" in r["why"], r["why"])

print("\n== a gated repository, and a missing package ==")
r = tri.triage(GATED)
check("HuggingFace's 401 is read as gating, not as a broken app",
      r["cause"] == "gated_weights", str(r["cause"]))
check("with the repository named from the real body",
      r["fix"]["repo"] == "meta-llama/Llama-2-7b", str(r["fix"]))
check("and the fix points at the shared credentials",
      r["fix"]["action"] == "credentials", str(r["fix"]))

r = tri.triage(NO_MODULE)
check("a missing import is read as an environment problem",
      r["cause"] == "missing_package" and r["fix"]["module"] == "insightface", str(r["fix"]))
check("the fix is the app's own installer, never a bare pip install",
      r["fix"]["action"] == "fix_app" and "pip" not in r["fix"]["detail"], str(r["fix"]))

print("\n== a real system-memory failure stays a system-memory failure ==")
check("an honest MemoryError is a RAM failure, not a card failure",
      tri.triage(REAL_MEMORY_ERROR)["cause"] == "out_of_ram")

print("\n== when the log does not name a cause, nothing is invented ==")
r = tri.triage("Starting server\nRunning on local URL: http://127.0.0.1:8188\n* Running on all addresses\n")
check("no signature means no cause", r["found"] is False and r["cause"] is None)
check("and the tail is handed back rather than a guess",
      any("127.0.0.1:8188" in line for line in r["tail"]), str(r["tail"]))

print("\n== a log with two problems keeps both ==")
r = tri.triage(NO_MODULE + "loading\n" + MISSING_WEIGHT, {})
check("the specific reading is the headline",
      r["cause"] == "missing_weights", str(r["cause"]))
check("and the other one is still named, with its line",
      [a["cause"] for a in r["also"]] == ["missing_package"], str(r["also"]))

print("\n== a huge log is read by its tail, cheaply ==")
big = ("noise line\n" * 90_000) + CUDA_OOM
r = tri.triage(big, {})
check("a multi-megabyte log still answers", r["cause"] == "cuda_oom", str(r["cause"]))
check("and says how much it read rather than implying it read everything",
      r["scanned"]["bytes"] <= tri.SCAN_BYTES, str(r["scanned"]))

# ---------------------------------------------------- the route and the health

print("\n== the route, against a home with a real crash in it ==")
home = Path(tempfile.mkdtemp(prefix="athena-triage-"))
app_dir = home / "api" / "box.git"
# A complete-enough app, so the only thing under test is the reading: the health
# checks about code and manifests have to pass for "not reported broken" to mean
# anything.
(app_dir / "app").mkdir(parents=True)
(app_dir / "app" / "start.js").write_text("console.log(1)\n", encoding="utf-8")
(app_dir / "pinokio.js").write_text("module.exports = {}\n", encoding="utf-8")
log_dir = home / "logs" / "shell" / "cleaned" / "api" / "box.git"
log_dir.mkdir(parents=True)
(log_dir / "start.js.0.txt").write_text(MISSING_WEIGHT, encoding="utf-8")

# The home is settings-driven rather than settable, so the test moves the setting
# the way a real install would — every accessor downstream (engine.home, the app
# lookup, the log reader) then agrees on where Athena is, because it is one
# accessor. Patching the attribute is impossible on purpose: it is a property.
real_settings_file = api.SETTINGS_FILE
settings = Path(tempfile.mkdtemp(prefix="athena-triage-settings-")) / "settings.json"
settings.write_text('{"home": "%s", "port": 42001}' % home.as_posix(), encoding="utf-8")
api.SETTINGS_FILE = settings
try:
    res = api.app_triage("box.git").body
    import json as _json
    data = _json.loads(res)
    check("the route reads the app's log off disk and names the cause",
          data.get("cause") == "missing_weights", str(data)[:200])
    check("and does not dump the whole log unless asked",
          "tail" not in data, str(list(data)))
    with_tail = _json.loads(api.app_triage("box.git", lines=1).body)
    check("asking for lines gets the tail too",
          with_tail.get("tail") and "checkpoints" in with_tail["tail"][-1], str(with_tail.get("tail")))

    health = api._app_health("box.git")
    names = [c["name"] for c in health["checks"]]
    check("the health report carries the failure", health.get("failure") is not None, str(names))
    check("and a past failure sits next to the action that answers it",
          health["failure"]["fix"]["action"] == "point_models", str(health["failure"]["fix"]))
    last = [c for c in health["checks"] if c["name"] == "Last failure"]
    check("the check is informational — a log is a record, not a diagnosis",
          bool(last) and last[0]["informational"] is True, str(last))
    # The invariant, not the fixture: a failure held as `informational` cannot move
    # the verdict. Asserting `health["ok"] is True` here would only be testing that
    # this synthetic home happens to be complete, which is a different claim.
    verdict_without_it = all(c["ok"] for c in health["checks"] if not c["informational"])
    check("so a failure that is history cannot make an app look broken",
          health["ok"] is verdict_without_it, f'{health["ok"]} vs {verdict_without_it}')

    empty = home / "api" / "quiet.git"
    (empty / "app").mkdir(parents=True)
    (empty / "app" / "start.js").write_text("console.log(1)\n", encoding="utf-8")
    (empty / "pinokio.js").write_text("module.exports = {}\n", encoding="utf-8")
    data = _json.loads(api.app_triage("quiet.git").body)
    check("an app that has said nothing says so instead of blaming something",
          data.get("found") is False and "no output" in data.get("why", ""), str(data)[:200])

    missing = api.app_triage("nope.git")
    check("an app that is not there is a 404, not a reading",
          missing.status_code == 404, str(missing.body)[:120])
finally:
    api.SETTINGS_FILE = real_settings_file

print(f"\n{PASS} passed, {FAIL} failed")
if FAILURES:
    print("failed: " + ", ".join(FAILURES))
sys.exit(1 if FAIL else 0)
