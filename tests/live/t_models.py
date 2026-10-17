"""The answer to "cannot generate": folders this machine already has weights in.

An app with no weights of its own is not broken, and on a machine whose weights
live in one place per person it is the normal state of every fresh install. What
turns that into a dead end is Athena only reporting it, so the verdict carries a
step — and the step needs a folder to name, which should be one that provably
holds weights rather than a path somebody has to remember.

Discovery is the part that can be wrong quietly: it reads what installed apps are
already pointed at, and the parser it reuses answers with the *mapped leaf*
(`.../models/checkpoints`) rather than the library that holds it. Offering the
leaf would map an app to a folder that has no `checkpoints/` inside it. This test
is here because that mistake was made once and found only by asking the real
machine.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
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


api = load("athena_api_under_test", PLUGIN / "dashboard" / "plugin_api.py")

home = Path(tempfile.mkdtemp(prefix="athena-models-"))
api_root = home / "api"
api_root.mkdir(parents=True)


def make_app(name: str) -> Path:
    d = api_root / name
    (d / "app").mkdir(parents=True, exist_ok=True)
    return d


def make_library(path: Path, checkpoints: int = 1, unet: int = 0) -> Path:
    """A weights folder shaped the way ComfyUI expects one."""
    for key, n in (("checkpoints", checkpoints), ("unet", unet)):
        (path / key).mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (path / key / f"{key}-{i}.safetensors").write_bytes(b"x" * 32)
    return path


def point_at(app: Path, library: Path) -> None:
    """Give an app ComfyUI's own extra-paths file, as a pointed app has."""
    (app / "app" / "extra_model_paths.yaml").write_text(
        "athena_shared:\n"
        f"    base_path: {library.as_posix()}\n"
        "    checkpoints: checkpoints\n"
        "    unet: unet\n", encoding="utf-8")


print("== a folder an installed app can already generate from is offered ==")
shared = make_library(home / "weights", checkpoints=3, unet=2)
pointed = make_app("A.git")
point_at(pointed, shared)

cands = api._model_library_candidates(home)
folders = [c["folder"] for c in cands]
check("the library an app is pointed at is the one offered",
      len(cands) == 1 and folders[0] == shared.as_posix(), str(folders))
check("and it is the library, not the leaf the app's file maps to",
      not any(f.endswith("/checkpoints") for f in folders), str(folders))
check("it says how much generating weight is there, per folder",
      cands[0]["counts"] == {"checkpoints": 3, "unet": 2, "diffusion_models": 0},
      str(cands[0]["counts"]))
check("and which app proved it, so the offer can name its own evidence",
      cands[0]["apps"] == ["A"], str(cands[0]["apps"]))

print("\n== a folder with no generating weights is not offered ==")
empty = make_library(home / "empty-weights", checkpoints=0)
(point_at(make_app("B.git"), empty))
cands = api._model_library_candidates(home)
check("an app pointed at an empty library offers nothing",
      [c["folder"] for c in cands] == [shared.as_posix()], str([c["folder"] for c in cands]))

print("\n== a folder named in settings counts too, without an app to prove it ==")
spare = make_library(home / "spare-weights", checkpoints=1)
settings = home / "settings.json"
settings.write_text(json.dumps({"model_roots": [spare.as_posix()]}), encoding="utf-8")
real_settings_file = api.SETTINGS_FILE
api.SETTINGS_FILE = settings
try:
    cands = api._model_library_candidates(home)
    by_folder = {c["folder"]: c for c in cands}
    check("a spare library is offered even though no app points at it",
          by_folder.get(spare.as_posix(), {}).get("counts", {}).get("checkpoints") == 1,
          str(list(by_folder)))
    check("with no app named, because there is none",
          by_folder.get(spare.as_posix(), {}).get("apps") == [],
          str(by_folder.get(spare.as_posix(), {}).get("apps")))
    check("and the biggest library comes first",
          [c["folder"] for c in cands][0] == shared.as_posix(),
          str([c["folder"] for c in cands]))
finally:
    api.SETTINGS_FILE = real_settings_file

print("\n== the shelf: what each app can generate with, and what nothing reads ==")
# The per-app view answers "can this app generate". The question a person asks
# next — what is this disk full of, what does this box still lack, what may be
# deleted — is about all of it at once. Same data, other end.
lone = make_app("C.git")                      # its own models/ folders, no mapping
for key in ("checkpoints", "unet", "diffusion_models"):
    (lone / "app" / "models" / key).mkdir(parents=True, exist_ok=True)
(lone / "app" / "models" / "loras").mkdir(parents=True, exist_ok=True)
(lone / "app" / "models" / "loras" / "style.safetensors").write_bytes(b"x" * 64)

settings.write_text(json.dumps({"model_roots": [spare.as_posix()]}), encoding="utf-8")
hub = home / "cache" / "HF_HOME" / "hub" / "models--someone--thing"
(hub / "blobs").mkdir(parents=True, exist_ok=True)
(hub / "blobs" / "abc.bin").write_bytes(b"y" * 4096)
hub_snapshot = hub / "snapshots" / "rev1"
hub_snapshot.mkdir(parents=True, exist_ok=True)
try:
    (hub_snapshot / "thing.safetensors").symlink_to(hub / "blobs" / "abc.bin")
except OSError:
    pass

api.SETTINGS_FILE = settings
try:
    inv = api._weights_inventory(home)
finally:
    api.SETTINGS_FILE = real_settings_file

by_app = {a["app"]: a for a in inv["apps"]}
check("every installed app gets a verdict", set(by_app) == {"A", "B", "C"}, str(sorted(by_app)))
check("the app that can generate says so",
      by_app["A"]["ready"] and by_app["A"]["count"] == 5, json.dumps(by_app["A"])[:160])
check("and the app that cannot carries the step that would fix it",
      by_app["A"]["ready"] is True and by_app["C"]["next_step"]["action"] == "point_models",
      json.dumps(by_app["C"])[:160])
check("an app with no mapping still names the folders it looked at",
      any("checkpoints" in f for f in by_app["C"]["folders"]), str(by_app["C"]["folders"]))

lib_by_folder = {l["folder"]: l for l in inv["libraries"]}
check("each library says which app reads it",
      lib_by_folder.get(shared.as_posix(), {}).get("apps") == ["A"],
      str(lib_by_folder.get(shared.as_posix(), {}).get("apps")))
check("an app's own three models folders are one library, not three",
      sum(1 for f in lib_by_folder if f.startswith(lone.as_posix())) == 1,
      str([f for f in lib_by_folder if f.startswith(lone.as_posix())]))
check("a library holding bytes but no generating weights says which it is",
      "cannot generate" in (lib_by_folder.get((lone / "app" / "models").as_posix(), {})
                            .get("note", "")),
      json.dumps(lib_by_folder.get((lone / "app" / "models").as_posix(), {}))[:200])

unread = [u["folder"] for u in inv["unreferenced"]]
check("a library no app points at is named as unread", spare.as_posix() in unread, str(unread))
check("with the size attached, because that is the whole reason it matters",
      all(u["bytes"] > 0 for u in inv["unreferenced"] if u["folder"] == spare.as_posix()),
      json.dumps(inv["unreferenced"]))
check("the app folders that are legitimately empty are not called unread",
      not any("api/B" in u for u in unread), str(unread))

cached = inv["cached"]
check("a HuggingFace repository on this disk is reported by id, not by folder name",
      [r["repo"] for r in cached["repos"]] == ["someone/thing"],
      json.dumps(cached)[:200])
check("a repository's size is the weights it holds",
      cached["bytes"] == 4096 and cached["repos"][0]["bytes"] == 4096,
      json.dumps(cached["repos"]))
if (hub_snapshot / "thing.safetensors").is_symlink():
    # A HuggingFace snapshot is a tree of links into the blobs beside it, so a
    # walk that followed them would report every byte twice.
    check("and its snapshot links add nothing on top of those blobs",
          cached["bytes"] == 4096, json.dumps(cached["repos"]))
else:
    print("  skip  this account may not create symlinks, so the double-count "
          "cannot be exercised here")
check("a cached repository is not called unread — an app reads it by asking for it",
      not any("HF_HOME" in u for u in unread), str(unread))
check("and the totals agree with the lists they summarise",
      inv["totals"]["unreferenced_bytes"] == sum(u["bytes"] for u in inv["unreferenced"])
      and inv["totals"]["cached_repos"] == cached["count"]
      and inv["totals"]["apps"] == len(inv["apps"]),
      json.dumps(inv["totals"]))
check("the summary is a sentence, because an agent reads this first",
      isinstance(inv["summary"], str) and "apps can generate" in inv["summary"],
      inv["summary"])

unsized = api._weights_inventory(home, sizes=False)
check("asking without sizes skips the weighing rather than guessing at it",
      all("bytes" not in l for l in unsized["libraries"]),
      str([sorted(l) for l in unsized["libraries"]][:2]))

print()
if FAILURES:
    print(f"{FAIL} check(s) failed")
    for f in FAILURES:
        print("  FAIL", f)
else:
    print(f"{PASS}/{PASS} checks passed")
sys.exit(1 if FAIL else 0)
