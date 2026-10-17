"""What Athena calls its own files, and how it still reads someone else's app.

Athena's launcher is `athena.js`. The vendored engine resolves the legacy name
literally (`kernel/api/index.js` builds the path from the string), so an app in
Athena's home carries both: the real launcher, and a one-line shim under the
legacy name that re-exports it. The engine is never patched, so an engine update
cannot quietly change what Athena means by "the launcher".

Nothing else in Athena should spell either filename. Ask here, or a second
answer drifts in and an app that runs in one code path 404s in another — which is
exactly how the store once published apps the catalogue could not install.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable, Optional

# The launcher Athena writes, and the legacy name the engine insists on.
LAUNCHER = "athena.js"
LEGACY_LAUNCHER = "pinokio.js"
LAUNCHER_NAMES = (LAUNCHER, LEGACY_LAUNCHER)

# The nested shape the engine also probes for.
NESTED_LAUNCHERS = ("athena/athena.js", "pinokio/pinokio.js")

# Metadata. The engine writes the legacy names itself on install; Athena prefers
# its own and reads the legacy ones when that is all there is.
META = "athena_meta.json"
LEGACY_METAS = ("pinokio_meta.json", "pinokio.json")
META_NAMES = (META,) + LEGACY_METAS

# Scripts an app may define, in menu order.
SCRIPT_NAMES = ("install.js", "start.js", "update.js", "reset.js")

# The shim is one line because it must never be mistaken for the launcher.
SHIM_BODY = (
    "// Athena launcher shim: the engine resolves this filename, Athena reads\n"
    "// athena.js. Generated — edit the launcher, not this file.\n"
    'module.exports = require("./athena.js");\n'
)


def launcher_of(app_dir: Path) -> Optional[Path]:
    """The app's launcher: `athena.js` if it exists, else whatever legacy file.

    Returns None when the folder has no launcher at all — which is the honest
    answer for "this is not an app", and the caller decides what to say about it.
    """
    app_dir = Path(app_dir)
    for name in LAUNCHER_NAMES:
        p = app_dir / name
        if p.is_file():
            return p
    for rel in NESTED_LAUNCHERS:
        p = app_dir / rel
        if p.is_file():
            return p
    return None


def launcher_name_of(app_dir: Path) -> Optional[str]:
    p = launcher_of(app_dir)
    return p.name if p else None


def has_launcher(app_dir: Path) -> bool:
    return launcher_of(app_dir) is not None


def meta_of(app_dir: Path) -> Optional[Path]:
    """The app's metadata file, Athena's name first (the engine writes its own)."""
    app_dir = Path(app_dir)
    for name in META_NAMES:
        p = app_dir / name
        if p.is_file():
            return p
    return None


def is_launcher_name(name: str) -> bool:
    return name in LAUNCHER_NAMES or name in NESTED_LAUNCHERS


def write_shim(app_dir: Path) -> Path:
    """Write (or refresh) the legacy-named re-export beside the launcher."""
    app_dir = Path(app_dir)
    shim = app_dir / LEGACY_LAUNCHER
    shim.write_text(SHIM_BODY, encoding="utf-8", newline="\n")
    return shim


def write_meta(app_dir: Path, meta: dict) -> Path:
    import json

    app_dir = Path(app_dir)
    p = app_dir / META
    p.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")
    return p


def script_path(app_dir: Path, name: str) -> Optional[Path]:
    """One of the app's own scripts, if it defines it."""
    p = Path(app_dir) / name
    return p if p.is_file() else None


def env_aliases(base: Iterable[str] = ()) -> dict:
    """Athena's env names for a script, so an app never has to learn the legacy ones.

    The engine exports its own names into every app command; Athena's are added
    alongside rather than replacing them, because an unconverted app still reads
    the old ones and must keep working.
    """
    out = {"ATHENA_HOME": "", "ATHENA_SCRIPT_AUTOLAUNCH": "", "ATHENA_SCRIPT_REQUIRES": ""}
    out.update({k: v for k, v in (base or [])})
    return out

# The engine is vendored, never forked: its own strings still name the vendor.
# They reach a person through Athena's log view and error text, so the rename
# happens on the way out, here. A URL is left alone — it points at something
# real, and rewriting it would hand someone a link that goes nowhere.
_URL = re.compile(r"https?://\S+")


def tracked_by_git(p: Path) -> bool:
    """True when the surrounding repository tracks this file — i.e. it is theirs.

    Git is the authority, not a guess about who wrote a file. An app transferred
    from elsewhere is a checkout whose files are all tracked; a file Athena wrote
    sits outside anyone's repository.
    """
    p = Path(p)
    parent = p.parent
    if not (parent / ".git").exists():
        return False
    try:
        r = subprocess.run(["git", "-C", str(parent), "ls-files", "--error-unmatch",
                            p.name], capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        return True      # cannot tell → assume theirs, and leave it alone
    return r.returncode == 0


def ensure_meta(folder: Path) -> str:
    """Put Athena's own name on the metadata, when the metadata is ours.

    The engine writes its own metadata beside the launcher, so an app that was
    built here can end up carrying only that file — a legacy name, in an app that
    has nothing to do with the engine's origin. A file git does not track is ours
    to rename; a tracked one belongs to the project and is left alone.
    """
    folder = Path(folder)
    ours = folder / META
    legacy = [folder / n for n in LEGACY_METAS if (folder / n).is_file()]
    mine = [p for p in legacy if not tracked_by_git(p)]
    if ours.is_file():
        dropped = 0
        for p in mine:
            try:
                p.unlink()
                dropped += 1
            except OSError:
                pass
        return "dropped-legacy-duplicate" if dropped else "already"
    if not legacy:
        return "no-meta"
    if not mine:
        return "upstream"
    try:
        mine[0].rename(ours)
    except OSError:
        return "kept-legacy"
    for p in mine[1:]:
        try:
            p.unlink()
        except OSError:
            pass
    return "renamed"

_SCRUB_RULES = (
    ("pinokio_meta.json", "athena_meta.json"),
    ("pinokio/pinokio.js", "athena/athena.js"),
    ("pinokio.json", "athena.json"),
    ("pinokio.js", "athena.js"),
    ("pinokio://", "athena://"),
    ("pinokio_configs", "athena_configs"),
    ("pinokiod", "athenad"),
    ("PINOKIO_", "ATHENA_"),
    (".pinokio", ".athena"),
    ("Pinokio", "Athena"),
    ("pinokio", "athena"),
)


def scrub_engine_text(text: str) -> str:
    """Translate the engine's words into Athena's, for text a person will read."""
    if not text:
        return text
    kept: list[str] = []
    out = _URL.sub(lambda m: kept.append(m.group(0)) or f"\x00{len(kept) - 1}\x00", text)
    for a, b in _SCRUB_RULES:
        out = out.replace(a, b)
    for i, s in enumerate(kept):
        out = out.replace(f"\x00{i}\x00", s)
    return out
