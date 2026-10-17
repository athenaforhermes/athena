"""Finding Node, and finding a port. No engine, no app, no routes.

Split out of `plugin_api.py` (see rule 136). This is the first thing the engine asks
about a machine -- *is there a Node here, and where* -- and it had been sitting in the
middle of nine thousand lines of routes, which made it the one piece of plumbing nobody
could read on its own.

It needs nothing from the API module: no engine instance, no settings, no router. The
one piece of state is the 60-second memo below, which belongs to whoever is asking, so
it stays here with the lookup rather than being passed in.

Imported by name, because `plugin_api.py` is itself loaded by path and a relative import
there resolves at import time and fails at call time (rule 136).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------- node runtime

# Hermes downloads the toolchains it needs into a per-platform tools directory:
# `%LOCALAPPDATA%/hermes/tools` on Windows, `~/.local/share/hermes/tools` on
# Linux, `~/Library/Application Support/hermes/tools` on macOS. Athena runs the
# engine on Node 22 because pinokiod's native addons (node-pty, better-sqlite3)
# only ship prebuilds up to Node 24 — Node 26 fails both install and boot.
# Nothing here may name a particular machine or a particular OS: the list is
# walked on a stranger's box too, and a candidate that exists nowhere costs one
# glob. `HERMES_TOOLS` (or `ATHENA_NODE_DIR`) points Athena at a toolchain
# somewhere else, which is what a machine with its own Node wants.
def tool_roots() -> List[Path]:
    roots: List[Path] = []
    for var in ("HERMES_TOOLS", "ATHENA_NODE_DIR"):
        got = os.environ.get(var)
        if got:
            roots.append(Path(got))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "hermes" / "tools")
    roots += [
        Path.home() / ".local" / "share" / "hermes" / "tools",
        Path.home() / "Library" / "Application Support" / "hermes" / "tools",
        Path.home() / ".hermes" / "tools",
    ]
    return roots


def bundled_nodes() -> List[Path]:
    """Every Node Hermes has downloaded, highest version first.

    Sorted on the version in the directory name, so a machine holding both 22 and
    26 offers 26 first and `_valid_node` rejects it on the ABI rather than the pin
    being trusted blindly."""
    out: List[Path] = []
    for root in tool_roots():
        try:
            dirs = sorted(root.glob("node-*"), reverse=True)
        except Exception:
            continue
        for d in dirs:
            exe = d / ("node.exe" if os.name == "nt" else "bin" + os.sep + "node")
            try:
                if exe.exists():
                    out.append(exe)
            except Exception:
                continue
    return out


def valid_node(exe: Any) -> Optional[str]:
    """`exe` as a string if pinokiod's native addons have a prebuild for it.

    The ABI is the only version fact that matters here: 127 is Node 22 and 137 is
    Node 24, and Node 25/26 (ABI 141+) has no node-pty prebuild, so it is refused
    rather than started and left to fail later."""
    try:
        out = subprocess.run([str(exe), "-p", "process.versions.modules"],
                             capture_output=True, text=True, timeout=10)
        abi = int((out.stdout or "0").strip() or 0)
        if abi and abi <= 137:
            return str(exe)
    except Exception:
        pass
    return None


_NODE_MEMO: Dict[str, Any] = {"key": None, "at": 0.0, "path": None}


def find_node() -> Optional[str]:
    """The Node 22 runtime, found once and remembered for a minute.

    `status()` reports it on every poll, and the search runs `node --version` on
    each candidate — a subprocess spawned by a route the panel hits every five
    seconds, for an answer that only changes if Node is installed or removed
    mid-session. Keyed on ATHENA_NODE so an override still takes effect at once,
    and a failed search is never cached.
    """
    key = os.environ.get("ATHENA_NODE") or ""
    now = time.time()
    if (_NODE_MEMO["path"] and _NODE_MEMO["key"] == key
            and now - _NODE_MEMO["at"] < 60.0):
        return _NODE_MEMO["path"]
    found = find_node_uncached()
    if found:
        _NODE_MEMO.update(key=key, at=now, path=found)
    return found


def find_node_uncached() -> Optional[str]:
    explicit = os.environ.get("ATHENA_NODE")
    if explicit:
        ok = valid_node(explicit)
        if ok:
            return ok
    for exe in bundled_nodes():
        ok = valid_node(exe)
        if ok:
            return ok
    for name in ("node22", "node"):
        found = shutil.which(name)
        if found:
            ok = valid_node(found)
            if ok:
                return ok
    return None


def find_free_port(start: int, tries: int = 40) -> Optional[int]:
    """The first port at or above `start` that nothing is listening on.

    Deliberately a plain bind with no SO_REUSEADDR: on Windows SO_REUSEADDR lets
    a socket bind a port another process is actively listening on, which would
    report a busy port as free and send the engine straight back into
    EADDRINUSE.
    """
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                # Probe the address the engine will actually bind. A port can
                # be free on loopback and held elsewhere; probing 0.0.0.0 would
                # report it busy and skip a port that would have worked.
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


