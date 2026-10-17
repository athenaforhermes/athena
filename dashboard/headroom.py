"""
Is there room for this?

Three resources, one question each, each asked at the moment it can still change
the answer:

* disk — before an install, because an app's install downloads a repository and
  builds a Python or Node environment into it, and the failure mode of running
  out halfway is a half-built app rather than an error;
* VRAM — before a launch, because a card that is nearly full does not refuse
  politely, it OOMs whichever app loaded second;
* host memory — before starting a *machine*, because a guest is sized from the
  host's **total** RAM (`machines.fit_spec`) and the host does not care what the
  total is: a box whose memory is already committed boots the guest and then
  thrashes, or kills the desktop session that was holding it.

None of these is a prediction. Athena cannot know what an app will need — nothing
in these apps declares a port or a model size — so the disk and VRAM gates report
the current numbers and the user's own thresholds, and say so. Two refusals are
hard: a disk with less free space than an install needs to write anything at all,
and a machine asked to start on a host with too little memory free to hold it.

The thresholds live in settings (`disk_warn_free_gb`, `vram_warn_free_gb`,
`machine_min_free_mb`); 0 turns any of them off. Nothing in this module writes
files or starts processes.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

GB = 1024 ** 3

# Below this, an install cannot write its own environment, let alone the app's.
# Deliberately not a setting: it is not a preference, it is arithmetic.
FLOOR_GB = 2.0


def _volume(path: Any) -> str:
    """`C:` on Windows, `/` on a POSIX box, for a sentence a person reads."""
    try:
        anchor = Path(str(path)).resolve().anchor
    except Exception:  # noqa: BLE001
        return str(path)
    return anchor.rstrip("\\/") or anchor


def disk(path: Any) -> Optional[Dict[str, Any]]:
    """Free/total space on the volume holding `path`, or None if unreadable."""
    try:
        usage = shutil.disk_usage(str(path))
    except Exception:  # noqa: BLE001
        return None
    return {
        "volume": _volume(path),
        "free_bytes": int(usage.free),
        "free_gb": round(usage.free / GB, 1),
        "total_gb": round(usage.total / GB, 1),
        "used_pct": round(100.0 * usage.used / max(1, usage.total), 1),
    }


def install_gate(path: Any, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Refuse or warn before an install writes into `path`.

    Returns `{"ok": True}` when there is room and no warning is wanted, `ok` with
    a `message` when the user's own threshold says to speak up, and
    `{"ok": False, "reason": "no_disk", ...}` when there is genuinely no room.
    """
    cfg = cfg or {}
    d = disk(path)
    if not d:
        return {"ok": True}
    out: Dict[str, Any] = {"ok": True, "free_gb": d["free_gb"], "volume": d["volume"]}
    if d["free_gb"] < FLOOR_GB:
        return {
            **out, "ok": False, "reason": "no_disk",
            "floor_gb": FLOOR_GB,
            "message": (f"{d['volume']} has {d['free_gb']} GB free — an install writes a "
                        f"repository, a Python or Node environment and usually a model into "
                        f"this drive, and needs at least {FLOOR_GB:g} GB before it can start "
                        f"writing anything"),
        }
    try:
        warn = float(cfg.get("disk_warn_free_gb", 10.0) or 0)
    except (TypeError, ValueError):
        warn = 10.0
    if warn > 0 and d["free_gb"] < warn:
        out["warn_gb"] = warn
        out["message"] = (
            f"{d['volume']} has {d['free_gb']} GB free, under the {warn:g} GB you set — an "
            f"app's install downloads and builds into this drive, so a big one can run out "
            f"of room partway through")
    return out


def gpu(cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Current VRAM numbers, or None where there is no card to ask.

    Reads the same one `nvidia-smi` call the system monitor uses rather than
    starting a second sampler: two samplers disagree, and then nobody believes
    either of them.
    """
    try:
        import system as _system  # dashboard/ is on sys.path (see plugin_api)
    except Exception:  # noqa: BLE001
        return None
    metrics = getattr(_system, "_gpu_metrics", None)
    if not metrics:
        return None
    m = metrics()
    if not m:
        return None
    total = float(m.get("mem_total_mib") or 0) / 1024.0
    used = float(m.get("mem_used_mib") or 0) / 1024.0
    if total <= 0:
        return None
    return {"name": m.get("name"), "total_gb": round(total, 1),
            "used_gb": round(used, 1), "free_gb": round(max(0.0, total - used), 1),
            "util": m.get("util")}


def gpu_gate(cfg: Optional[Dict[str, Any]] = None,
             running: Iterable[str] = ()) -> Optional[Dict[str, Any]]:
    """Would launching another app now be a coin flip on the card?

    None when there is nothing worth saying (no card, thresholds off, or room
    to spare), otherwise the numbers plus what is holding them. It does not
    attempt to know what the app needs — it reports headroom and lets the user
    decide, which is the only honest thing to do with numbers this coarse.
    """
    cfg = cfg or {}
    try:
        warn_gb = float(cfg.get("vram_warn_free_gb", 6.0) or 0)
    except (TypeError, ValueError):
        warn_gb = 6.0
    if warn_gb <= 0:
        return None
    g = gpu()
    if not g or g["free_gb"] >= warn_gb:
        return None
    holders = [name for name in (running or []) if name]
    who = ""
    if holders:
        who = f" ({', '.join(sorted(holders))} {'is' if len(holders) == 1 else 'are'} running)"
    return {
        # `holders` is also spelled out in the message; it is a list here because a
        # caller that wants to act on it (a queued launch, a panel that offers to
        # stop the other app) should not have to parse a sentence for names.
        **g, "warn_gb": warn_gb, "holders": sorted(holders),
        "message": (f"only {g['free_gb']} GB of the card's {g['total_gb']} GB is free right "
                    f"now{who} — an app that loads a model may fail to load, or push "
                    f"another app out"),
    }


# A guest has to at least boot. Below this the host is not a host — it is a machine
# already using its memory, and a QEMU process that cannot fault in its pages will
# thrash against the swap rather than fail cleanly. Not a setting for the same reason
# FLOOR_GB is not: arithmetic, not preference.
MEMORY_FLOOR_MB = 512


def memory() -> Optional[Dict[str, Any]]:
    """Total/available host memory in MB, or None where it cannot be read.

    Reads through `machines` rather than psutil directly so that there is one number
    in the plugin and therefore one number to argue with: the sizing code and the
    refusal that guards it must never disagree about how much memory this box has.
    """
    try:
        import machines as _machines  # dashboard/ is on sys.path (see plugin_api)
    except Exception:  # noqa: BLE001
        return None
    total = _machines._total_memory_mb()
    free = _machines._free_memory_mb()
    if total is None and free is None:
        return None
    out: Dict[str, Any] = {"total_mb": total, "free_mb": free}
    if total:
        out["used_pct"] = round(100.0 * (total - (free or 0)) / total, 1)
    return out


def machine_gate(cfg: Optional[Dict[str, Any]] = None,
                 running: Iterable[str] = ()) -> Dict[str, Any]:
    """Refuse to start a guest on a host that has already spent its memory.

    The guest's own size is not consulted. It cannot be: a machine is configured
    before it starts, and the number that decides whether this is a good moment is
    how much memory the *host* has left — a 2 GB guest on a box with 300 MB free
    fails exactly as hard as a 16 GB one.

    `running` is the names of the screens already up, named in the refusal so the
    user can see what is holding the memory. Nothing here stops anything: refusing
    to add load is a decision about the future, and taking down a screen somebody is
    watching is a decision about the present, which is theirs to make.
    """
    cfg = cfg or {}
    try:
        min_free = int(float(cfg.get("machine_min_free_mb", 1536) or 0))
    except (TypeError, ValueError):
        min_free = 1536
    if min_free <= 0:
        return {"ok": True}
    m = memory()
    if not m or m.get("free_mb") is None:
        # An unreadable number is not a reason to refuse anything — it is a reason to
        # say nothing and let the start proceed, which is what every other gate does.
        return {"ok": True}
    free, total = int(m["free_mb"]), m.get("total_mb")
    out: Dict[str, Any] = {"ok": True, "free_mb": free, "total_mb": total}
    holders = sorted({name for name in (running or []) if name})
    who = (f" ({', '.join(holders)} {'is' if len(holders) == 1 else 'are'} up)"
           if holders else "")
    floor = max(MEMORY_FLOOR_MB, min_free)
    if free < floor:
        total_s = f" of {round(total / 1024, 1)} GB" if total else ""
        return {
            **out, "ok": False, "reason": "no_memory", "floor_mb": floor,
            "holders": holders,
            "message": (f"only {free} MB{total_s} is free right now{who} — a guest is "
                        f"sized from this machine's total memory, not what is left, so "
                        f"starting one now would leave the host thrashing or would kill "
                        f"whatever is holding that memory. Close something, or lower "
                        f"the floor, and try again"),
        }
    return out
