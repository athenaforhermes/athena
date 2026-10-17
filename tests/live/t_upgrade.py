#!/usr/bin/env python3
"""An upgrade path is a settings file written by a version that no longer exists.

Athena used to keep `settings.json` in the plugin folder. It moved into Athena's own
home (so the plugin directory holds code and nothing else), and the old location is
still read — `LEGACY_SETTINGS_FILE` — which is the whole of the migration. Four things
go wrong quietly, and every one of them shows up as a *setting that reverted*, which
reads like the user mis-clicking rather than like a bug:

  1. The old file stops being read. A person upgrades and their port, or the folder
     they pointed the ISO picker at, is back to the default.
  2. A save writes a fresh file at the new location that carries only the keys the
     caller happened to be holding. The old keys are gone the moment anything is saved.
  3. A save deletes a key it was never told about — a newer version's key, or
     `iso_dir`, which the machines tab writes and a save from the engine tab must not
     remove. (The rule is `None` means remove; absent means leave alone.)
  4. The two places that resolve the settings disagree. `system.py` cannot import
     `plugin_api.py` (the import direction is a cycle) so it resolves the same files
     itself, and a resolution that reads whole files while the other reads keys answers
     differently the first time the two files hold different keys.

Run with `env -u PYTHONPATH -u ATHENA_HOME -u ATHENA_PORT` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

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


# The fixture home is set *before* the imports, because both modules resolve their
# settings path at import time and the whole point of this file is which path that is.
TMP = Path(tempfile.mkdtemp(prefix="athena-upgrade-")).resolve()
os.environ["ATHENA_HOME"] = str(TMP)
os.environ.pop("ATHENA_PORT", None)

PLUGIN_DIR = Path(os.environ.get("ATHENA_PLUGIN") or HERMES_ROOT / "plugins" / "athena")


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


api = load("athena_api_upgrade_test", PLUGIN_DIR / "dashboard" / "plugin_api.py")
sysmod = load("athena_system_upgrade_test", PLUGIN_DIR / "dashboard" / "system.py")

NEW = TMP / "settings.json"
OLD = TMP / "legacy-settings.json"
api.SETTINGS_FILE = NEW
api.LEGACY_SETTINGS_FILE = OLD
sysmod.SETTINGS_FILE = NEW
sysmod.LEGACY_SETTINGS_FILE = OLD

# What 0.53 wrote, before the file moved: the keys that existed then. `iso_dir` is
# the machines tab's own key and is deliberately in here — it is the key an unrelated
# save from another tab has already deleted once in this project's history.
FIFTY_THREE = {
    "home": str(TMP).replace("\\", "/"),
    "port": 42700,
    "autostart": False,
    "share_caches": True,
    "idle_stop_seconds": 900,
    "store_index": "https://example.invalid/store",
    "disk_warn_free_gb": 4.0,
    "vram_warn_free_gb": 2.0,
    "iso_dir": str(TMP / "isos"),
}


def reset(new=None, old=None) -> None:
    for p in (NEW, OLD):
        if p.exists():
            p.unlink()
    if new is not None:
        NEW.write_text(json.dumps(new, indent=2), encoding="utf-8")
    if old is not None:
        OLD.write_text(json.dumps(old, indent=2), encoding="utf-8")


def disk() -> dict:
    return api._settings_on_disk()


# --------------------------------------------------------------- 1. the old file is read

def old_file_is_read() -> bool:
    reset(old=dict(FIFTY_THREE))
    got = api._load_settings()
    return (got["port"] == 42700 and got["idle_stop_seconds"] == 900
            and got["autostart"] is False and got["iso_dir"] == FIFTY_THREE["iso_dir"])


def new_file_wins_key_by_key() -> bool:
    """Both files present: the new one answers for the keys it holds, and only those."""
    reset(new={"port": 42800}, old=dict(FIFTY_THREE))
    got = api._load_settings()
    return got["port"] == 42800 and got["idle_stop_seconds"] == 900


def defaults_fill_what_neither_file_has() -> bool:
    reset(old={"port": 42700})
    got = api._load_settings()
    return got["port"] == 42700 and got["idle_stop_seconds"] == api.DEFAULTS["idle_stop_seconds"]


# ------------------------------------------------------- 2. a save carries the old keys

def save_carries_the_old_keys() -> bool:
    reset(old=dict(FIFTY_THREE))
    api._save_settings({"idle_stop_seconds": 300})
    written = json.loads(NEW.read_text(encoding="utf-8"))
    return (written.get("iso_dir") == FIFTY_THREE["iso_dir"]
            and written.get("store_index") == FIFTY_THREE["store_index"]
            and written.get("autostart") is False
            and written.get("idle_stop_seconds") == 300)


def save_keeps_the_old_file_in_place() -> bool:
    """A save writes the new location; it does not delete what the old version reads."""
    reset(old=dict(FIFTY_THREE))
    api._save_settings({"idle_stop_seconds": 300})
    return OLD.is_file() and json.loads(OLD.read_text(encoding="utf-8")) == FIFTY_THREE


# ------------------------------------------------- 3. unknown keys, `None`, idempotence

def unknown_key_survives_a_save() -> bool:
    reset(old={**FIFTY_THREE, "something_from_the_future": {"kept": True}})
    api._save_settings({"port": 42900})
    written = json.loads(NEW.read_text(encoding="utf-8"))
    return written.get("something_from_the_future") == {"kept": True} and written["port"] == 42900


def none_removes_and_absent_leaves_alone() -> bool:
    reset(old={"port": 42700, "iso_dir": str(TMP / "isos")})
    api._save_settings({"iso_dir": None})
    written = json.loads(NEW.read_text(encoding="utf-8"))
    return "iso_dir" not in written and written.get("port") == 42700


def saving_twice_is_the_same_file() -> bool:
    reset(old=dict(FIFTY_THREE))
    api._save_settings({"port": 42900})
    once = NEW.read_bytes()
    api._save_settings({"port": 42900})
    return once == NEW.read_bytes()


def a_corrupt_file_is_not_fatal() -> bool:
    reset()
    OLD.write_text("{ this is not json", encoding="utf-8")
    NEW.write_text("[]", encoding="utf-8")          # valid JSON, wrong shape
    try:
        got = api._load_settings()
    except Exception as exc:                        # noqa: BLE001 - that is the failure
        return False
    return got["port"] == api.DEFAULTS["port"]


# ---------------------------------------------------- 4. both resolvers, one answer

def both_resolvers_agree() -> bool:
    cases = [
        ({}, {}),
        ({}, {"port": 42700}),
        ({"port": 42800}, {"port": 42700}),
        ({"idle_stop_seconds": 300}, {"port": 42700}),          # new file, no port
        ({"port": "not-a-port"}, {"port": 42700}),
    ]
    for new, old in cases:
        reset(new=new or None, old=old or None)
        mine = api._load_settings()["port"]
        theirs = sysmod.engine_port()
        if mine != theirs:
            print(f"       both_resolvers_agree: new={new} old={old} -> "
                  f"plugin_api {mine}, system {theirs}")
            return False
    return True


def a_typed_env_does_not_erase_a_legacy_port() -> bool:
    """The upgrade case of the typo rule: the fallback has to reach the old file."""
    reset(old=dict(FIFTY_THREE))
    os.environ["ATHENA_PORT"] = "4270o"
    try:
        mine = api._load_settings()["port"]
        theirs = sysmod.engine_port()
    finally:
        os.environ.pop("ATHENA_PORT", None)
    return mine == 42700 and theirs == 42700


# ---------------------------------------------------------------------------- the checks

print("== an upgrade keeps what the old version wrote ==")
check("the settings file in the plugin folder is still read", old_file_is_read())
check("with both files present, the new one answers for the keys it has, and only those",
      new_file_wins_key_by_key())
check("defaults fill in what neither file mentions", defaults_fill_what_neither_file_has())
check("a save carries every key the old file held", save_carries_the_old_keys())
check("a save leaves the old file where the old version reads it", save_keeps_the_old_file_in_place())
check("a key this version has never heard of survives a save", unknown_key_survives_a_save())
check("`None` removes a key and an absent key is left alone",
      none_removes_and_absent_leaves_alone())
check("saving the same change twice writes the same file", saving_twice_is_the_same_file())
check("a corrupt settings file falls back to the defaults instead of raising",
      a_corrupt_file_is_not_fatal())
check("the two resolvers answer the same port for the same files", both_resolvers_agree())
check("a typo in ATHENA_PORT still falls through to the old file's port",
      a_typed_env_does_not_erase_a_legacy_port())

# The control. A checker for a merge cannot be trusted until something proves it
# notices a merge that does not happen: the naive implementation reads only the file
# at the new location and writes only what its caller held. Both of the scenarios that
# exist to catch that have to fail under it.
print("\n== control: the naive implementation fails these ==")
# Patch the module that owns the read, not the API's wrapper around it: `save_settings` calls
# `settings_on_disk` as a name inside `settings_store`, so patching `api._settings_on_disk` would
# leave the real reader in place and the control would pass while proving nothing (rule 152).
real_reader = api.athena_settings.settings_on_disk
api.athena_settings.settings_on_disk = lambda legacy, settings: {}   # "read the new file only"
try:
    check("control — a save that drops the old keys is caught", not save_carries_the_old_keys())
    check("control — a save that drops an unknown key is caught", not unknown_key_survives_a_save())
finally:
    api.athena_settings.settings_on_disk = real_reader

print(f"\n{PASS}/{PASS + FAIL} checks passed")
if FAILURES:
    print("failing: " + "; ".join(FAILURES))
sys.exit(1 if FAIL else 0)
