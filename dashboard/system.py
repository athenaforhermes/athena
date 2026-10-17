"""Athena system monitor — the machine, mounted at /api/plugins/athena/sys/.

Read-only: psutil counters plus nvidia-smi when it is present. No writes, no
secrets. Network up/down are per-second deltas between consecutive calls, so the
first call after start reports zeros.

Network is reported the way someone actually asks about it: the current rate per
interface, the totals since boot, and the last few minutes of throughput so the
UI can draw a shape instead of a number. A single rate answers "is it busy",
which is the least interesting question — "did that download finish, and on
which adapter" needs the total and the name.

Ported from the standalone system monitor with three changes that matter for an
app store rather than a status widget:

  * every real drive is reported, not just C:. Models and app installs land on
    other volumes, and "the disk is full" is only actionable if it names which.
  * per-app RAM/VRAM is proxied from the engine (`/resource-usage/workspace/…`)
    instead of being measured here. The engine already tracks which app a
    process belongs to; duplicating that would mean guessing from process trees.
  * an app's usage is only reported when the engine has it running, so a stopped
    app reads as stopped rather than as zero usage.

Costs are cached by lifetime, not re-measured on every poll. The status-bar mini
monitor calls /stats every 3 seconds, and this route is not cheap: each _gpu()
shells out to nvidia-smi TWICE (Windows pays a driver-init cost per call) and
_disks() asks every volume for its free space. Uncached that pinned a worker for
~2.3s out of every 3 and made the whole plugin feel sluggish. Each source now
gets a TTL matching how fast it can actually change, so a poll normally answers
from memory and the subprocess work no longer repeats twice a second.

A TTL alone was not enough. A cold key still ran its subprocess *on the request*,
and two polls landing together both ran it, so one slow nvidia-smi (it blocks
for seconds whenever the GPU is busy) became a stack of them — long enough that
the desktop gave up on the backend and the app read as frozen. So: cached reads
are single-flight, and nvidia-smi is sampled on a background thread that the
request path only ever reads.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
from collections import deque
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable, Dict, List, Optional

import psutil
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()

# plugins/athena — same resolution as the file browser's sandbox.
PLUGIN_DIR = Path(__file__).resolve().parent.parent

# Which engine this tab reads.
#
# This was a constant, and the port is the user's to set: a settings key, or
# `ATHENA_PORT` for one run. Reading 42001 whatever the rest of the plugin had
# been told meant a second Athena on any other port showed the System tab's app
# rows as though the engine were down while it was running perfectly well.
#
# `plugin_api.py` is not imported to reuse its answer — it imports *this* module
# to mount the router, so the direction would be a cycle. The same two places are
# read in the same order instead, and `t_system.py` holds the two resolutions
# against each other so letting them drift fails the suite rather than only this
# tab. A port is not a preference worth two answers.
DEFAULT_ENGINE_PORT = 42001
ENGINE = "http://127.0.0.1:42001"      # the default, and what the docstrings quote

# Settings live beside Athena's home, resolved exactly as plugin_api resolves it:
# `ATHENA_HOME` for one run, then the shipped default, with the plugin folder's
# own file as the legacy location.
SETTINGS_FILE = Path(os.environ.get("ATHENA_HOME")
                     or PLUGIN_DIR.parent.parent / "athena") / "settings.json"
LEGACY_SETTINGS_FILE = PLUGIN_DIR / "settings.json"

_last_net: Optional[tuple] = None
_last_nic: Dict[str, tuple] = {}

# Throughput history for the graph in the UI. Appended on every read (the /stats
# TTL is 2s, so samples land 2-3 a second apart while anyone is watching), and
# bounded so a long session costs a fixed few kilobytes. Idle costs nothing:
# nothing polls, nothing is appended.
_NET_HISTORY: "deque[List[int]]" = deque(maxlen=120)

# Sample cache: key -> (monotonic timestamp, value). A UI poll must never be the
# reason a subprocess runs, so anything with a startup cost is read through here.
# TTLs stay short on purpose: these are live readings, and a 5-second-old GPU
# number is honest, while recomputing it 200 times a minute is just waste.
_CACHE: Dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()
_INFLIGHT: Dict[str, threading.Event] = {}


def _cached(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    """Cached read with a single-flight guard.

    Two pollers arriving on a cold key both used to call `fn()`, so a slow
    source was paid for twice — and the second call started while the first was
    still stuck. That pile-up, not the cost of one call, is what stalled the
    plugin. Now one caller computes and the rest wait for that same result, so
    the source runs once no matter how many polls land together.

    Waiting is bounded by a single call, which is the whole point: N pollers
    cost one slow source, not N of them.
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        ev = _INFLIGHT.get(key)
        if ev is None:
            ev = threading.Event()
            _INFLIGHT[key] = ev
            leader = True
        else:
            leader = False
    if not leader:
        ev.wait(timeout=30)
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        return hit[1] if hit is not None else None
    try:
        val = fn()
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), val)
        return val
    finally:
        with _CACHE_LOCK:
            _INFLIGHT.pop(key, None)
        ev.set()


def _athena_home() -> Path:
    """Athena's home, read from settings.json so there is one answer, not two."""
    h = PLUGIN_DIR.parent.parent / "athena"
    try:
        cfg = json.loads((PLUGIN_DIR / "settings.json").read_text(encoding="utf-8"))
        v = (cfg or {}).get("home")
        if v:
            h = Path(str(v).replace("\\", "/"))
    except Exception:
        pass
    try:
        return h.resolve()
    except Exception:
        return h


def _mb(n: int) -> float:
    """Bytes as MB, which is what a total is actually read in."""
    return round(max(0, int(n)) / (1024 ** 2), 1)


def _net_ifaces(cur_nics: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
    """Per-interface rates and totals, busiest first.

    Loopback and interfaces that have never carried a byte are left out: on a
    desktop with a VPN, a virtual switch and a Wi-Fi adapter that is not the one
    connected, listing them all buries the adapter that is actually moving the
    traffic. A dead interface is not information.
    """
    global _last_nic
    try:
        stats = psutil.net_if_stats()
    except Exception:
        stats = {}

    rows: List[Dict[str, Any]] = []
    for name, c in (cur_nics or {}).items():
        low = name.lower()
        if "loopback" in low or low.startswith("lo"):
            continue
        prev = _last_nic.get(name)
        dt = max(now - prev[0], 0.001) if prev else 0.0
        up_bps = int(max(0, (c.bytes_sent - prev[1]) / dt)) if prev else 0
        down_bps = int(max(0, (c.bytes_recv - prev[2]) / dt)) if prev else 0
        st = stats.get(name)
        rows.append({
            "name": name,
            "up_bps": up_bps,
            "down_bps": down_bps,
            "sent_mb": _mb(c.bytes_sent),
            "recv_mb": _mb(c.bytes_recv),
            "speed_mbps": getattr(st, "speed", 0) or 0,
            "isup": bool(getattr(st, "isup", True)),
        })
    _last_nic = {name: (now, c.bytes_sent, c.bytes_recv)
                 for name, c in (cur_nics or {}).items()}
    rows = [r for r in rows
            if r["isup"] and (r["up_bps"] or r["down_bps"] or r["sent_mb"] or r["recv_mb"])]
    rows.sort(key=lambda r: (-(r["up_bps"] + r["down_bps"]),
                             -(r["sent_mb"] + r["recv_mb"]), r["name"].lower()))
    # What is moving, ranked. If nothing is moving -- a monitor opened after a
    # download finished -- the interface that has moved the most is still the one
    # worth naming. Either way an idle VPN or virtual switch is left out: a row
    # of zeros is not a reading, and three of them bury the adapter that matters.
    live = [r for r in rows if r["up_bps"] or r["down_bps"]]
    return (live or rows[:1])[:6]


def _net_bps() -> Dict[str, Any]:
    """Rates, totals, per-interface detail and a short history.

    The two keys the first version returned (`up_bps`/`down_bps`) stay exactly
    where they were: the status-bar chip and the ported panel read them, and a
    reading is only worth adding to if the old one still means the same thing.
    """
    global _last_net
    try:
        cur = psutil.net_io_counters()
        per = psutil.net_io_counters(pernic=True)
    except Exception:
        return {"up_bps": 0, "down_bps": 0, "nics": [], "history": []}
    now = time.time()
    if cur is None:
        return {"up_bps": 0, "down_bps": 0, "nics": [], "history": []}

    if _last_net is None:
        up_bps = down_bps = 0
    else:
        dt = max(now - _last_net[0], 0.001)
        up_bps = max(0, int((cur.bytes_sent - _last_net[1]) / dt))
        down_bps = max(0, int((cur.bytes_recv - _last_net[2]) / dt))
    _last_net = (now, cur.bytes_sent, cur.bytes_recv)
    _NET_HISTORY.append([down_bps, up_bps])

    nics = _net_ifaces(per, now)
    # The denominator a percentage should use is the link that is carrying the
    # traffic, not the fastest nominal one: a Tailscale or WSL adapter reports
    # 4294 Mb/s, which would make a fully-busy gigabit Wi-Fi read as 2%.
    fastest = (nics[0]["speed_mbps"] if nics else 0)
    return {
        "up_bps": up_bps,
        "down_bps": down_bps,
        # Since boot, as psutil counts them. Labelled by the UI as such: a total
        # that resets on reboot is not the same number as one that never does.
        "sent_mb": _mb(cur.bytes_sent),
        "recv_mb": _mb(cur.bytes_recv),
        "sent_bytes": int(cur.bytes_sent),
        "recv_bytes": int(cur.bytes_recv),
        "speed_mbps": fastest,
        "peak_down_bps": max((h[0] for h in _NET_HISTORY), default=0),
        "peak_up_bps": max((h[1] for h in _NET_HISTORY), default=0),
        "history": [list(h) for h in _NET_HISTORY],
        "nics": nics,
    }


def _disks() -> List[Dict[str, Any]]:
    # Free space moves slowly and this touches every volume, so a poller never
    # pays for it twice inside a quarter minute.
    return _cached("disks", 15.0, _disks_raw)


def _disks_raw() -> List[Dict[str, Any]]:
    """Every fixed/removable volume that reports a real size.

    Skipped silently when a drive is not ready (an empty card reader raises),
    which is the normal state on a desktop with card slots.
    """
    out: List[Dict[str, Any]] = []
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception:
        return out
    for part in parts:
        try:
            u = psutil.disk_usage(part.mountpoint)
            if not u.total:
                continue
            out.append({
                "mount": part.mountpoint,
                "device": part.device,
                "fstype": part.fstype,
                "percent": u.percent,
                "used_gb": round(u.used / (1024 ** 3), 1),
                "total_gb": round(u.total / (1024 ** 3), 1),
                "free_gb": round(u.free / (1024 ** 3), 1),
            })
        except Exception:
            continue
    out.sort(key=lambda d: d["mount"].lower())
    return out


def _num(text: str) -> Optional[float]:
    """A number, or None where the card does not report one.

    nvidia-smi answers "[N/A]" — not an error — for anything the driver cannot
    read: power draw on a card without the sensor, a limit that does not apply.
    A zero put in its place would be a claim about the machine, so this keeps the
    absence instead and lets the readers say they do not know.
    """
    try:
        return float(text)
    except Exception:  # noqa: BLE001
        return None


def _gpu_metrics() -> Optional[Dict[str, Any]]:
    """One nvidia-smi call: the numbers for the first GPU.

    Power draw rides along in the same call rather than a second one: the point
    of sampling this on a thread was that nvidia-smi talks to the driver and can
    block for seconds, and a second call per sample would double that exposure to
    answer a question the first call already had in hand.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
             "temperature.gpu,power.draw,power.limit",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        )
        parts = [p.strip() for p in out.stdout.strip().split(",")]
        if len(parts) < 5:
            return None
        util = float(parts[1])
        return {
            "name": parts[0],
            "util": util,
            # Whether the card is being asked to work at all. The one honest line
            # for "in use" that nvidia-smi offers: a card holding VRAM for a
            # loaded model it is not running reads as zero, which it is.
            "busy": util > 0,
            "mem_used_mib": float(parts[2]),
            "mem_total_mib": float(parts[3]),
            "temp_c": float(parts[4]),
            # Power is the one reading here a card is allowed to not have. It is
            # kept as an absence rather than a zero standing in for it.
            "power_w": _num(parts[5]) if len(parts) > 5 else None,
            "power_limit_w": _num(parts[6]) if len(parts) > 6 else None,
        }
    except Exception:
        return None


def _gpu_procs() -> List[str]:
    """The second nvidia-smi call, kept apart so it can go stale much more
    slowly than the utilisation it sits next to."""
    procs: List[str] = []
    try:
        p2 = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8,
        )
        for line in p2.stdout.strip().splitlines():
            try:
                procs.append(psutil.Process(int(line.strip())).name())
            except Exception:
                pass
    except Exception:
        pass
    return sorted(set(procs))[:8]


# ---------------------------------------------------------------- gpu sampler
# nvidia-smi is the one reading here that can block for seconds: it talks to the
# driver, and while the GPU is busy — an app being killed, VRAM being released —
# it hangs until its timeout. On the request path that turned a 3-second poll
# into a stall the desktop's own 8-second API timeout could not survive, and a
# cold TTL let the next poll start a second nvidia-smi while the first was still
# stuck, so they stacked. It is sampled on its own thread now and the route only
# reads the last snapshot, so a poll can no longer be the reason it runs at all.
_SAMPLE: Dict[str, Any] = {}
_SAMPLE_LOCK = threading.Lock()
_SAMPLE_LAST_READ = 0.0
_SAMPLER_ON = False
_SAMPLE_INTERVAL = 3.0        # utilisation moves in seconds
_SAMPLE_PROCS_EVERY = 5       # the attached-process list does not
_SAMPLE_TOPS_EVERY = 5        # nor does a ranking by memory
_SAMPLE_IDLE_STOP = 30.0      # stop once nothing has read a sample for this long
_SAMPLE_TOP_LIMIT = 6         # the only depth the payload asks for

# What else wants each sample as it is taken. The cost record is one: it is a
# running total of the card's own time and power, and the only place that reading
# exists is here. A hook is deliberately not allowed to break the sampler — a
# record-keeping failure must not take the monitor down with it.
_SAMPLE_HOOKS: List[Callable[[Dict[str, Any]], None]] = []


def on_sample(fn: Callable[[Dict[str, Any]], None]) -> None:
    """Register something to be handed every sample of the card, as it is taken."""
    if fn not in _SAMPLE_HOOKS:
        _SAMPLE_HOOKS.append(fn)


def _sampler_loop() -> None:
    global _SAMPLER_ON
    tick = 0
    while True:
        try:
            m = _gpu_metrics()
            if m is not None:
                with _SAMPLE_LOCK:
                    _SAMPLE["metrics"] = m
                for hook in list(_SAMPLE_HOOKS):
                    try:
                        hook(m)
                    except Exception:  # noqa: BLE001
                        pass
            if tick % _SAMPLE_PROCS_EVERY == 0:
                # Computed outside the lock: holding it across the subprocess is
                # how a slow nvidia-smi would block the readers again.
                procs = _gpu_procs()
                with _SAMPLE_LOCK:
                    _SAMPLE["procs"] = procs
            if tick % _SAMPLE_TOPS_EVERY == 0:
                # Walking the process table costs ~1.5s on this machine. Cached
                # at 5s it still landed on the poll path every few seconds; on
                # the sampler it never does.
                tops = _top_procs_raw(_SAMPLE_TOP_LIMIT)
                with _SAMPLE_LOCK:
                    _SAMPLE["top"] = tops
        except Exception:  # noqa: BLE001
            pass
        tick += 1
        with _SAMPLE_LOCK:
            if time.monotonic() - _SAMPLE_LAST_READ > _SAMPLE_IDLE_STOP:
                _SAMPLER_ON = False
                return
        time.sleep(_SAMPLE_INTERVAL)


def _ensure_sampler() -> None:
    global _SAMPLER_ON
    with _SAMPLE_LOCK:
        if _SAMPLER_ON:
            return
        _SAMPLER_ON = True
    threading.Thread(target=_sampler_loop, name="athena-gpu-sampler",
                     daemon=True).start()


def _gpu() -> Optional[Dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return None
    global _SAMPLE_LAST_READ
    with _SAMPLE_LOCK:
        _SAMPLE_LAST_READ = time.monotonic()
        metrics = _SAMPLE.get("metrics")
        procs = _SAMPLE.get("procs") or []
    _ensure_sampler()
    if metrics is None:
        # First tick, or the sampler is still inside a slow nvidia-smi. Answering
        # "no reading yet" costs the UI one blank tick; blocking here costs it
        # the whole app.
        return None
    gpus = [{**metrics, "procs": procs}]
    # Keep the first GPU's fields at the top level too: a single-GPU machine is
    # the common case and the UI should not have to index into a list.
    return {**gpus[0], "gpus": gpus}


def _top_procs(limit: int = 6) -> List[Dict[str, Any]]:
    # The full process-table walk is the other half of the cost, and a ranking by
    # memory does not reorder second to second. The sampler keeps the depth the
    # payload actually asks for; anything else (nothing today) falls back to the
    # bounded cached path rather than silently returning nothing.
    if limit == _SAMPLE_TOP_LIMIT:
        global _SAMPLE_LAST_READ
        with _SAMPLE_LOCK:
            _SAMPLE_LAST_READ = time.monotonic()
            tops = _SAMPLE.get("top")
        _ensure_sampler()
        if tops is not None:
            return tops
    return _cached(f"top:{limit}", 5.0, lambda: _top_procs_raw(limit))


def _top_procs_raw(limit: int = 6) -> List[Dict[str, Any]]:
    rows = []
    for p in psutil.process_iter(["pid", "name", "memory_percent"]):
        try:
            info = p.info
            rows.append({
                "name": info.get("name") or f"pid {info.get('pid')}",
                "mem": round(info.get("memory_percent") or 0, 1),
            })
        except Exception:
            pass
    rows.sort(key=lambda r: r["mem"], reverse=True)
    return rows[:limit]


# One keep-alive connection per worker thread. The apps list asks the engine
# once per installed app and the UI polls it every few seconds, so opening a
# fresh socket per call left ~100 sockets in TIME_WAIT on the engine's port at
# any moment — a fresh handshake, handler invocation and teardown about once a
# second, which is the kind of thing that reads as a periodic CPU blip.
# Thread-local because FastAPI runs sync routes in a thread pool and a shared
# connection would interleave two responses into a corrupt stream.
_engine_tls = threading.local()

# The resolved target, cached for a few seconds: this is asked once per engine
# call and answering it means two file stats and a JSON parse. Rebuilt rather than
# frozen so a settings change lands without restarting Hermes.
_ENGINE_TARGET: Dict[str, Any] = {"at": 0.0, "url": "", "parts": None}
_ENGINE_TTL = 5.0


def engine_port() -> int:
    """The engine's port, in the order plugin_api resolves it: env, file, default."""
    env = os.environ.get("ATHENA_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    for path in (SETTINGS_FILE, LEGACY_SETTINGS_FILE):
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("port")
        except Exception:
            continue
        try:
            if value:
                return int(value)
        except (TypeError, ValueError):
            continue
    return DEFAULT_ENGINE_PORT


def engine_url() -> str:
    """The engine's base URL, as one string, for anything that wants to show it."""
    return f"http://127.0.0.1:{engine_port()}"


def _engine_target() -> tuple:
    """(url, SplitResult) — what every engine call in this module goes through."""
    now = time.monotonic()
    if _ENGINE_TARGET["parts"] is not None and now - _ENGINE_TARGET["at"] < _ENGINE_TTL:
        return _ENGINE_TARGET["url"], _ENGINE_TARGET["parts"]
    url = engine_url()
    parts = urlsplit(url)
    _ENGINE_TARGET.update({"at": now, "url": url, "parts": parts})
    return url, parts


def _engine_get(path: str, timeout: float = 4.0) -> Optional[Any]:
    """Read the engine's HTTP surface, or None when it is not running.

    Retried once on a new connection, so an engine that restarted underneath a
    stale keep-alive reconnects instead of reading as down forever.
    """
    url, parts = _engine_target()
    conn = getattr(_engine_tls, "conn", None)
    if conn is not None and getattr(_engine_tls, "url", None) != url:
        # The engine moved. A kept-alive socket to the old port answers nothing
        # useful, and holding it would report the new one as down.
        try:
            conn.close()
        except Exception:
            pass
        conn = None
    _engine_tls.url = url
    for attempt in (0, 1):
        try:
            if conn is None:
                conn = http.client.HTTPConnection(
                    parts.hostname or "127.0.0.1", parts.port or 80, timeout=timeout)
                _engine_tls.conn = conn
            conn.timeout = timeout
            conn.request("GET", path)
            r = conn.getresponse()
            body = r.read()
            if r.status != 200:
                return None
            return json.loads(body.decode())
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            _engine_tls.conn = None
            if attempt:
                return None
    return None


def _engine_up() -> bool:
    """Liveness probe.

    `/health` is NOT an engine route — it answers an HTML 404 ("Cannot GET
    /health"), which `json.loads` turns into a failure and reports a running
    engine as down. `/info/scripts` is a real route and returns JSON.
    """
    return isinstance(_engine_get("/info/scripts", timeout=3.0), (dict, list))


@router.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "engine": _engine_up()}


def _stats_payload() -> Dict[str, Any]:
    try:
        mem = psutil.virtual_memory()
        freq = psutil.cpu_freq()
        payload = {
            "cpu": {
                "percent": psutil.cpu_percent(),
                "cores": psutil.cpu_count(),
                "freq_mhz": round(freq.current) if freq else None,
            },
            "mem": {
                "percent": mem.percent,
                "used_gb": round(mem.used / (1024 ** 3), 1),
                "total_gb": round(mem.total / (1024 ** 3), 1),
            },
            "disks": _disks(),
            "net": _net_bps(),
            "gpu": _gpu(),
            "boot_epoch": int(psutil.boot_time()),
            "top": _top_procs(),
        }
        return payload
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@router.get("/stats")
def stats() -> JSONResponse:
    # The mini monitor and an open System tab read the same numbers; the outer
    # TTL collapses them into one assembly so a second poller is nearly free.
    payload = _cached("stats", 2.0, _stats_payload)
    if not isinstance(payload, dict):
        # Only reachable if a single-flight waiter timed out. Saying "not ready"
        # beats raising on a poll, which is what an unguarded membership test
        # against None did.
        return JSONResponse({"error": "metrics not ready"}, status_code=503)
    return JSONResponse(payload, status_code=500 if "error" in payload else 200)


def _metric_bytes(row: Dict[str, Any], kind: str) -> int:
    """The engine's reported byte count for one metric, or 0 when it is absent.

    Reads only what the engine actually sent (`metrics.<kind>.bytes`). A stopped
    app carries `metrics: None` and so contributes nothing — that is the honest
    reading, not an invented zero-usage number for a process that is not there.
    """
    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        return 0
    node = metrics.get(kind)
    if not isinstance(node, dict):
        return 0
    value = node.get("bytes")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


@router.get("/apps")
def app_usage() -> JSONResponse:
    """Live RAM/VRAM per installed app, ranked so the biggest eater is first.

    One engine call per app: the route the engine's own UI polls. A stopped app
    answers `running: false` rather than erroring, and an app the engine has
    never seen simply has no metrics — both are reported as-is, because a card
    that shows 0 MB for a stopped app is a lie the UI would have to unpick.

    Ordering answers the question a user actually asks — "what is eating my
    memory right now?": running apps first, heaviest VRAM then heaviest RAM then
    name, with every stopped app after them regardless of the numbers it last
    reported. `totals` sums only the running apps, and `top` names the single
    heaviest running app so the UI can call it out without re-sorting itself.
    """
    home = _athena_home()
    apps: List[Dict[str, Any]] = []
    try:
        api = home / "api"
        folders = sorted((d for d in api.iterdir() if d.is_dir()),
                         key=lambda p: p.name.lower()) if api.is_dir() else []
    except Exception:
        folders = []

    engine_up = _engine_up()
    for d in folders:
        row: Dict[str, Any] = {"id": d.name, "running": False, "metrics": None}
        if engine_up:
            data = _engine_get(f"/resource-usage/workspace/{d.name}")
            if isinstance(data, dict) and data:
                row["running"] = bool(data.get("running"))
                row["stale"] = bool(data.get("stale"))
                row["metrics"] = data.get("metrics")
                row["preferences"] = data.get("preferences")
        apps.append(row)

    # Rank: running apps by VRAM then RAM then name; every stopped app after
    # them. A stopped app has no meaningful usage, so it never outranks one that
    # is running, whatever it last reported.
    running_apps = [a for a in apps if a["running"]]
    stopped_apps = [a for a in apps if not a["running"]]
    running_apps.sort(key=lambda a: (-_metric_bytes(a, "vram"),
                                     -_metric_bytes(a, "ram"),
                                     a["id"].lower()))
    stopped_apps.sort(key=lambda a: a["id"].lower())
    ranked = running_apps + stopped_apps

    # Totals count only real reported values from apps that are actually
    # running; the stopped apps above contribute nothing.
    totals = {
        "ram_bytes": sum(_metric_bytes(a, "ram") for a in running_apps),
        "vram_bytes": sum(_metric_bytes(a, "vram") for a in running_apps),
        "running": len(running_apps),
    }
    top = None
    if running_apps:
        head = running_apps[0]
        top = {
            "id": head["id"],
            "ram_bytes": _metric_bytes(head, "ram"),
            "vram_bytes": _metric_bytes(head, "vram"),
        }

    return JSONResponse({
        "ok": True,
        "engine": engine_up,
        "count": len(ranked),
        "running": len(running_apps),
        "apps": ranked,
        "totals": totals,
        "top": top,
    })
