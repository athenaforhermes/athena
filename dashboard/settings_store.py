"""Where the settings live, and how a save goes on top of what is already there.

Split out of `plugin_api.py` (rule 136). The file *paths* deliberately stayed in the API
module: three harnesses redirect a test run by patching `api.SETTINGS_FILE`, so the store
takes the path as a parameter and the API's wrappers hand over whatever its global holds
at call time. A store that owned the path itself would have made those tests silently
write to the real settings file -- rule 152's trap, one module over.

Imported by name, because `plugin_api.py` is itself loaded by path (rule 136).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


# ---------------------------------------------------------------- settings

# Settings live in Athena's home, not beside the source. A plugin directory is
# code: `hermes plugins update` replaces it wholesale, so a setting written there
# is a setting that disappears on the next update — and one that travelled with
# the repository besides. The old location is still read, once, so an existing
# install keeps what it had; nothing writes there any more.
def as_port(value: Any) -> Optional[int]:
    """A port number, or None when the value is not one.

    `int()` used to run straight at settings load, so an `ATHENA_PORT` with a
    typo in it was fatal: every route raised, and the panel showed an engine that
    was "down" for a reason written nowhere it could be read. A value that is not
    a port is not a port — it falls through to the settings file, then to the
    default, and the plugin is still there to say so.
    """
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def settings_home(plugin_dir: Path) -> Path:
    env = os.environ.get("ATHENA_HOME")
    return Path(env) if env else plugin_dir.parent.parent / "athena"



_DEFAULTS_TEMPLATE: Dict[str, Any] = {
    # Keep Athena's home inside the Hermes data directory: everything Athena
    # installs lives under Hermes, so one folder is the whole story.
    "home": "",  # filled in by defaults() -- a home is a fact about the plugin dir
    "port": 42001,
    # Bring the engine up on its own. On by default: an app store you have to
    # switch on before it will show you anything is a store you stop opening.
    # Off leaves the engine down until Start is pressed.
    "autostart": True,
    # One copy of every model, wheel and hub bundle shared by all apps, instead
    # of one copy per app. On by default: it can only ever save space.
    "share_caches": True,
    # Stop an app nothing has used for this long. A loaded model can hold tens of
    # gigabytes of VRAM, and an app left serving its own UI after the job that
    # wanted it is finished is holding the card for nobody. 0 turns it off.
    "idle_stop_seconds": 120,
    # Where the Store reads its own apps from. Athena's store is a repository:
    # `main` holds the index, every other branch holds an app. An empty value
    # means Athena's own store; another URL replaces it, and the global
    # catalogue is still browsed behind it either way.
    "store_index": "",
    # Speak up before an install that starts on a nearly-full drive. Not a
    # prediction about the app — it is the user's own line, and 0 turns it off.
    "disk_warn_free_gb": 10.0,
    # Speak up before a launch when the card has almost nothing left. A loaded
    # model can hold tens of gigabytes, and the app that loads second is the one
    # that dies. 0 turns it off.
    "vram_warn_free_gb": 6.0,
    # Refuse to start a machine when the host has less than this much memory free.
    # A guest is sized from the host's *total* RAM, so on a box already using most
    # of its memory the guest boots into swap and takes the desktop with it.
    # Unlike the two above this one refuses rather than warns: a half-started
    # guest is worse than a start that never happened. 0 turns it off.
    "machine_min_free_mb": 1536,
}


def defaults(plugin_dir: Path) -> Dict[str, Any]:
    """The settings a fresh install starts from, with the home filled in."""
    cfg = dict(_DEFAULTS_TEMPLATE)
    cfg["home"] = str(plugin_dir.parent.parent / "athena").replace("\\", "/")
    return cfg


def settings_on_disk(legacy_file: Path, settings_file: Path) -> Dict[str, Any]:
    """Every settings file as written, no defaults and no environment folded in.

    The old file is read first and the new one laid over it **key by key**, because this
    migration has to be able to be partial: a file at the new location that happens to
    hold only a port must not hide the ISO folder the old file still holds. Reading the
    first readable file whole did exactly that, and it is the one place this resolution
    differed from `system.py`, which reads the same two files per key — the two answers
    disagreed the first time a key was in one file and not the other, and `t_upgrade.py`
    asks both for the same number.

    A file that is not an object is not a settings file: `[]` parses as JSON, has no
    keys, and skipping it is what stops one bad file taking the rest with it.

    Kept apart from `load_settings()` because a save has to merge onto this, not onto
    the resolved answer: writing resolved values back would freeze an `ATHENA_HOME`
    that was only meant to last one run.
    """
    merged: Dict[str, Any] = {}
    for path in (legacy_file, settings_file):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(body, dict):
            merged.update(body)
    return merged


def port_from_files(settings_file: Path, legacy_file: Path) -> Optional[int]:
    """The first file holding a usable port, the new location first.

    Per file rather than off the merged answer, so a typo in one file falls through to
    the other instead of erasing it — the rule `ATHENA_PORT` already gets, one level
    down, because a settings file edited by hand is the other way this goes wrong.
    `system.py` resolves the same two files in this same order; `t_upgrade.py` requires
    the same number back from both.
    """
    for path in (settings_file, legacy_file):
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("port")
        except Exception:
            continue
        port = as_port(value)
        if port:
            return port
    return None


def load_settings(settings_file: Path, legacy_file: Path, defaults: Dict[str, Any]) -> Dict[str, Any]:
    cfg = dict(defaults)
    cfg.update(settings_on_disk(legacy_file, settings_file))
    if os.environ.get("ATHENA_HOME"):
        cfg["home"] = os.environ["ATHENA_HOME"]
    # Each source is *tested* before it is allowed to displace the one below it.
    # Assigning the environment blindly first and parsing afterwards meant a typo
    # in `ATHENA_PORT` did not fall through to a perfectly good port in the
    # settings file — it erased it, and the machine came up on the default while
    # the file still said otherwise. A value that is not a port is not an answer,
    # so it does not get to overwrite one.
    cfg["port"] = (as_port(os.environ.get("ATHENA_PORT"))
                   or port_from_files(settings_file, legacy_file)
                   or int(defaults["port"]))
    return cfg


def save_settings(cfg: Dict[str, Any], settings_file: Path, legacy_file: Path) -> None:
    """Save a change *onto* the file, never instead of it.

    This one file is shared: the engine keeps its own keys here (autostart,
    idle_stop_seconds, the disk warnings) and the machines tab keeps `iso_dir`. A save
    that wrote only what its caller was holding deleted everything else -- an ISO folder
    somebody had pointed at vanished the next time any unrelated setting was saved, and
    the panel then showed an empty list as if the folder had never been set.

    A caller that means to remove a key has to say so: **a key set to `None` is dropped**,
    a key simply absent is left alone, because "not mentioned" and "should be gone" are
    different answers and a merge has to be able to tell them apart.
    """
    merged = settings_on_disk(legacy_file, settings_file)
    for key, value in cfg.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    try:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    settings_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")


