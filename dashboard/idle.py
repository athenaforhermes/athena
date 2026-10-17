"""Idle accounting for Athena's apps — who is being used, and who is not.

Kept deliberately separate from `plugin_api.py` and free of any engine or HTTP
knowledge, because the two halves of this plugin load modules by path and each
one gets its OWN module object: the FastAPI router is imported by Hermes, while
the agent tools load their own copy. Anything they must share has to be loaded
once, by name, through `plugin_api._idle_module()` — so the state lives here and
both doors touch the same dict.

Only apps Hermes started are watched. An app the user started is theirs: they
opened it to use it and they will close it when they are done, so Athena leaves
it alone entirely. The window exists for the other case — an agent starts an app
to get something done and then stops using it, and without a window it would sit
there holding VRAM for the rest of the day. `watch()` is the only thing that arms
an app, and only the agent's own launch calls it.

What counts as using a watched app:

  * a call against it (launch, probe, generate, app_call),
  * a connection to its port,
  * a heartbeat from the viewer pane while it is open,
  * a hold: work in flight that outlives the request that started it (an app
    being booted, a job still running). A hold is what stops the window from
    running underneath a generation, however long the generation takes.

What deliberately does NOT: Athena's own status polling. The Apps tab asks after
every installed app every few seconds; counting that would mean nothing is ever
idle and this whole module would be decoration.

An app that is mid-call is never a candidate, however long the call runs — a
20-minute video generation is the busiest an app ever is, and reaping it there
would be worse than never reaping anything.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

# The window, in seconds, when nothing has said otherwise. Long enough that a
# person reading a result and deciding what to do next is not punished for it.
DEFAULT_SECONDS = 120

# Sessions shorter than this are not worth a sweep, and a negative or absurd
# value would reap an app the moment it launches.
MIN_SECONDS = 30
MAX_SECONDS = 24 * 3600

_state: Dict[str, Dict[str, Any]] = {}
_lock = threading.RLock()


def _norm(app_id: Any) -> str:
    name = str(app_id or "").strip()
    if name.lower().endswith(".git"):
        name = name[:-4]
    return name.lower()


def _row(name: str) -> Dict[str, Any]:
    """The record for an app, created unwatched. `setdefault` and not a fresh
    dict, so an armed app stays armed however often it is touched."""
    return _state.setdefault(name, {"last": 0.0, "watch": False, "holds": {}})


def _working(row: Dict[str, Any]) -> bool:
    """Is any work in flight against this app?

    A hold is taken by every caller that is doing something with the app: an
    `ac.call` (`begin`), an app being brought up, a job that outlives the
    request that started it. An idle window is about "nobody is doing anything
    with this", and a 20-minute generation is the busiest an app ever is; a
    clock that keeps running underneath it turns the window into a countdown to
    killing live work.
    """
    return bool(row.get("holds"))


def touch(app_id: Any, *, what: str = "", watch: Optional[bool] = None) -> None:
    """Note that something is using this app, now.

    `watch` arms or disarms the app — it is deliberately not implied by being
    touched. Athena's viewer heartbeats and a user's own launch touch an app
    without ever arming it, and only the agent's launch passes `watch=True`.
    """
    name = _norm(app_id)
    if not name:
        return
    with _lock:
        row = _row(name)
        if watch is not None:
            row["watch"] = bool(watch)
        row["last"] = time.time()
        row["what"] = what or row.get("what") or ""


def watch(app_id: Any, *, what: str = "") -> None:
    """Arm this app: Athena will stop it once it falls idle.

    Called where Hermes itself starts an app. Never for an app the user started
    from the UI — theirs to close, and a timeout on it is what this whole
    distinction exists to avoid.
    """
    touch(app_id, what=what, watch=True)


def unwatch(app_id: Any) -> None:
    """Disarm it without forgetting it — the app stays tracked, never reaped."""
    name = _norm(app_id)
    with _lock:
        row = _state.get(name)
        if row is not None:
            row["watch"] = False


def watched(app_id: Any) -> bool:
    with _lock:
        row = _state.get(_norm(app_id))
        return bool(row and row.get("watch"))


def hold(app_id: Any, what: str = "work") -> None:
    """Take a hold: work is in flight against this app and it is not idle.

    `what` names the kind of work (`"call"`, `"starting"`, `"job"`), so two
    callers taking a hold on the same app cannot release each other's, and one
    that is never released still leaves a name in `/idle` and in the log
    instead of a bare count. `release` is the other half and belongs in a
    `finally`.
    """
    name = _norm(app_id)
    if not name:
        return
    with _lock:
        row = _row(name)
        holds = row.setdefault("holds", {})
        key = str(what or "work")
        holds[key] = int(holds.get(key) or 0) + 1
        row["last"] = time.time()


def release(app_id: Any, what: str = "work") -> None:
    """The work ended — and the app was in use right up to the end."""
    name = _norm(app_id)
    if not name:
        return
    with _lock:
        row = _state.get(name)
        if row is None:
            return
        holds = row.get("holds") or {}
        key = str(what or "work")
        if int(holds.get(key) or 0) > 1:
            holds[key] = int(holds[key]) - 1
        else:
            holds.pop(key, None)
        row["last"] = time.time()


def begin(app_id: Any) -> None:
    """A call has started against this app; it is not idle until it ends."""
    hold(app_id, "call")


def end(app_id: Any) -> None:
    """The call ended — and it was in use right up to the end, so reset the clock."""
    release(app_id, "call")


def working(app_id: Any) -> bool:
    """Whether any work is in flight against this app right now."""
    with _lock:
        row = _state.get(_norm(app_id))
        return bool(row and _working(row))


def job_key(app_id: Any) -> str:
    """The name a job and an idle record are compared on: lower-case, no `.git`."""
    name = str(app_id or "").strip().lower()
    return name[:-4] if name.endswith(".git") else name


def jobs_named(app_id: Any, jobs: Iterable[Dict[str, Any]]) -> List[str]:
    """IDs of the running jobs in `jobs` that name this app.

    A job that names an app is work in flight against it, and the reaper reads
    this rather than trusting one code path to be the only way work happens:
    the runner holds the app itself, and this is the second, independent signal
    for a job that outlives the request which started it.
    """
    key = job_key(app_id)
    if not key:
        return []
    return sorted(str(j.get("id") or "") for j in jobs
                  if j.get("state") == "running" and job_key(j.get("app")) == key)


def users_on_port(port: int) -> int:
    """How many live connections `port` has right now — is anyone using it?

    Athena's own viewer is not the only way an app can be in use, and it is not
    even the usual way: an app opened in a second window, or in the browser its
    own launcher started, is being used while Athena sees nothing at all. A
    browser holding Gradio's websocket, a client waiting on a generation —
    either is a real connection to the app's port, and stopping the app
    underneath it is exactly the outcome this module exists to prevent.

    Windows only. Anywhere else (and on any error) this answers 0, which is the
    old behaviour: the heartbeat alone decides.
    """
    if os.name != "nt" or not port:
        return 0
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                             text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        return 0
    want = f":{port}"
    users = 0
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        #  TCP  local  foreign  ESTABLISHED  pid
        if len(parts) < 4 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() == "ESTABLISHED" and parts[1].endswith(want):
            users += 1
    return users


def forget(app_id: Any) -> None:
    """Stop tracking (the app was stopped on purpose, or is gone)."""
    with _lock:
        _state.pop(_norm(app_id), None)


def waiting(app_id: Any) -> float:
    """Seconds since this app was last used; 0.0 if it is not being tracked."""
    with _lock:
        row = _state.get(_norm(app_id))
        if not row:
            return 0.0
        return max(0.0, time.time() - float(row.get("last") or 0.0))


def due(seconds: float, now: Optional[float] = None) -> List[str]:
    """Apps that have gone quiet for longer than `seconds`, and are not busy.

    Returned oldest-quiet first, so a caller that only reaps one thing reaps the
    thing that has been idle longest.
    """
    if seconds is None or float(seconds) <= 0:
        return []
    now = time.time() if now is None else now
    with _lock:
        rows = [(name, now - float(row.get("last") or 0.0))
                for name, row in _state.items()
                if row.get("watch") and not _working(row)]
    rows = [(name, quiet) for name, quiet in rows if quiet > float(seconds)]
    rows.sort(key=lambda pair: pair[1], reverse=True)
    return [name for name, _ in rows]


def tracked() -> List[str]:
    with _lock:
        return sorted(_state)


def snapshot() -> List[Dict[str, Any]]:
    """Everything being tracked, with how long it has been quiet and whether a
    call is in flight — the shape a `/idle` route or a test wants."""
    now = time.time()
    with _lock:
        rows = []
        for name, row in sorted(_state.items()):
            rows.append({
                "app": name,
                "idle_seconds": round(max(0.0, now - float(row.get("last") or 0.0)), 1),
                "busy": _working(row),
                # What is in flight, by name: "call", "starting", a job's kind.
                "work": sorted((row.get("holds") or {}).keys()),
                # Armed = Hermes started it, so it will be stopped when idle.
                # Its absence is why an app the user started is never touched.
                "watched": bool(row.get("watch")),
                "what": row.get("what") or "",
            })
    return rows


def clamp(seconds: Any) -> float:
    """A user-supplied window, made safe. 0 means 'never reap' and stays 0."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return float(DEFAULT_SECONDS)
    if value <= 0:
        return 0.0
    return float(max(MIN_SECONDS, min(MAX_SECONDS, value)))


def reset() -> None:
    """Forget everything (tests)."""
    with _lock:
        _state.clear()
