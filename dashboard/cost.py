"""What this machine has actually spent on the graphics card.

A running record of two honest things: how long the card has been working, and
how much energy it has drawn. Both come from the sampler's own reading — its
utilisation and its power draw, straight from nvidia-smi — integrated over the
time between samples.

Kept in its own module on purpose. `plugin_api.py` is imported twice, once by
Hermes as a router and once by the agent tools by path, so the two halves hold
different module objects and any dict defined in there is two dicts. The record
must be one: a tool that read a halved total, or reset a copy nobody else could
see, would look correct and be wrong. Same reason `idle.py` exists.

Three limits, all of them stated rather than hidden:

* **Only time while Hermes was running is counted.** The sampler lives in the
  backend process and runs while something is watching the card (the desktop's
  own status bar keeps it alive); when Hermes is closed nothing is sampled, and a
  gap between two samples is *not* credited — see `_MAX_STEP`. So the total is
  "since this record began, while Hermes was open", never "since I bought the
  card".
* **The energy is an estimate from sampled power, not a meter.** A sample every
  few seconds times the watts read at that moment. It is right about the shape of
  a day's work and can be a few percent off on any given minute.
* **A card that does not report power draw gets time counted and energy left
  empty** (`watts` stays None, `kwh` stays None) rather than a zero standing in
  for a number nobody read.

Reached through `plugin_api._cost_module()`, which keys it in `sys.modules` under
`athena_cost` — the same door `idle.py` is opened through, for the same reason.
Never `import cost` and call it directly: that hands you whichever copy the
importing half happens to hold.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

# A sample every few seconds. Anything longer than this is a gap — Hermes closed,
# the machine asleep, a suspended process — and the card's power over that gap is
# unknown, so it is not credited. Sizing it at two intervals keeps one slow
# nvidia-smi from being read as a crash.
_MAX_STEP = 8.0

# Written on this cadence: often enough that a crash costs seconds, rare enough
# that a 3-second sampler is not rewriting a file 20 times a minute.
_FLUSH_EVERY = 30.0

# Reentrant on purpose: `reset` reads the price through `get_price`, which takes
# this same lock. A plain Lock would deadlock the route on itself — which it did,
# silently, until the suite timed out.
_LOCK = threading.RLock()
_STATE: Dict[str, Any] = {}
# Whether the monitor has already handed this record its samples. Two copies of
# plugin_api in one process (the router's and the agent tools') both call attach,
# and a second hook would fold every sample in twice — a total that is wrong by a
# factor the UI cannot see. The flag lives here, on the one shared module.
_ATTACHED = False


def _record_path(home: Path) -> Path:
    return Path(home) / "cache" / "gpu-cost.json"


def _blank() -> Dict[str, Any]:
    return {
        "since": None,
        "last": None,
        "seconds": 0.0,       # counted time, card idle or busy
        "busy_seconds": 0.0,  # of that, time with the card working
        "kwh": 0.0,           # energy drawn over the counted time, or None if unreadable
        "kwh_busy": 0.0,
        # Whether this card has ever reported a wattage. Energy is only reported
        # as a number when it has: a running total of 0.0 kWh on a card with no
        # power sensor reads as "you have used no electricity", which is a claim
        # nobody made.
        "power_seen": False,
        "watts": None,        # the last reading, for the UI to show
        "peak_watts": None,
        "samples": 0,
        "gaps": 0,            # gaps seen, so "why is this total small" has an answer
    }


def _load(home: Path) -> Dict[str, Any]:
    """The stored record, or a blank one — never an exception.

    A corrupt file is read as absent rather than raised: this is a running total
    kept as a courtesy, and losing it must not take a monitor panel down.
    """
    try:
        raw = json.loads(_record_path(home).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return _blank()
    except Exception:  # noqa: BLE001
        return _blank()
    out = _blank()
    for k in out:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
        elif k in ("kwh", "kwh_busy") and k in raw:
            out[k] = raw[k]        # None is a real value here: unreadable power
    if raw.get("price") is not None:
        out["price"] = raw["price"]
    return out


def _flush(home: Path, force: bool = False) -> None:
    now = time.monotonic()
    if not force and now - float(_STATE.get("_flushed") or 0) < _FLUSH_EVERY:
        return
    _STATE["_flushed"] = now
    payload = {k: v for k, v in _STATE.items() if not k.startswith("_")}
    try:
        p = _record_path(home)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(tmp, p)          # atomic: a half-written total is never read
    except Exception:  # noqa: BLE001
        pass


def _state(home: Path) -> Dict[str, Any]:
    global _STATE
    key = str(Path(home).resolve())
    if _STATE.get("_home") != key:
        _STATE = _load(Path(home))
        _STATE["_home"] = key
        _STATE["_last_mono"] = None
        _STATE["_flushed"] = 0.0
    return _STATE


def note(home: Path, metrics: Optional[Dict[str, Any]],
         now_mono: Optional[float] = None) -> None:
    """Fold one sample of the card into the record.

    Called from the sampler thread with what nvidia-smi just said. `util` is a
    percentage and `power_w` the draw in watts, either of which may be missing.
    """
    if not metrics:
        return
    now_mono = time.monotonic() if now_mono is None else now_mono
    with _LOCK:
        st = _state(home)
        prev = st.get("_last_mono")
        st["_last_mono"] = now_mono
        st["samples"] = int(st.get("samples") or 0) + 1
        watts = metrics.get("power_w")
        if isinstance(watts, (int, float)):
            st["watts"] = float(watts)
            peak = st.get("peak_watts")
            st["peak_watts"] = float(watts) if not isinstance(peak, (int, float)) \
                else max(float(peak), float(watts))
        if prev is None:
            if not st.get("since"):
                st["since"] = _iso()
            st["last"] = _iso()
            _flush(home)
            return
        dt = float(now_mono) - float(prev)
        if dt <= 0:
            # A backwards or stopped clock. The next sample re-anchors; nothing
            # is credited rather than a negative being folded in.
            return
        if dt > _MAX_STEP:
            # A gap: Hermes was not running, or the machine was asleep. Unknown
            # power, so unknown energy — counted as a gap, never as idle time.
            st["gaps"] = int(st.get("gaps") or 0) + 1
            st["last"] = _iso()
            _flush(home)
            return
        busy = bool(metrics.get("busy"))
        st["seconds"] = float(st.get("seconds") or 0) + dt
        if busy:
            st["busy_seconds"] = float(st.get("busy_seconds") or 0) + dt
        if isinstance(watts, (int, float)):
            st["power_seen"] = True
            # W·s to kWh is /3,600,000: watts times seconds is a watt-second, and
            # there are 3.6 million of them in a kilowatt-hour. Dividing by 3,600
            # (the trap — it is the right divisor for watt-hours) would report a
            # total a thousand times too large, and a cost to match.
            kwh_add = float(watts) * dt / 3600000.0
            st["kwh"] = float(st.get("kwh") or 0) + kwh_add
            if busy:
                st["kwh_busy"] = float(st.get("kwh_busy") or 0) + kwh_add
        st["last"] = _iso()
        if not st.get("since"):
            st["since"] = _iso()
        _flush(home)


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def summary(home: Path, price: Optional[float] = None) -> Dict[str, Any]:
    """The record as the panel reads it, with the words it may and may not say.

    `price` defaults to the one stored in the record rather than to zero: a caller
    that asks for the total and nothing else should not be told electricity here is
    free. Passing a price overrides the stored one, which is what `set_price` does
    with the value it just wrote.
    """
    with _LOCK:
        st = _state(home)
        seconds = float(st.get("seconds") or 0)
        busy = float(st.get("busy_seconds") or 0)
        kwh = st.get("kwh")
        kwh_busy = st.get("kwh_busy")
        samples = int(st.get("samples") or 0)
        since = st.get("since")
        gaps = int(st.get("gaps") or 0)
        watts = st.get("watts")
        peak = st.get("peak_watts")
        readable = bool(st.get("power_seen"))
        if price is None:
            try:
                price = max(0.0, float(st.get("price") or 0.0))
            except Exception:  # noqa: BLE001
                price = 0.0

    # A stored file can hold null here and a live record cannot, so the number is
    # only used when it is one.
    kwh_num = float(kwh) if isinstance(kwh, (int, float)) else None
    kwh_busy_num = float(kwh_busy) if isinstance(kwh_busy, (int, float)) else None
    readable = readable and kwh_num is not None
    # No price set is not a price of zero: money is left absent until someone says
    # what a kWh costs here, because "$0.00" is a number and this is not one.
    cost = None
    if readable and price:
        try:
            cost = round(float(kwh_num) * float(price), 4)
        except Exception:  # noqa: BLE001
            cost = None
    out: Dict[str, Any] = {
        "ok": True,
        "seconds": round(seconds, 1),
        "busy_seconds": round(busy, 1),
        "hours": round(seconds / 3600.0, 3),
        "busy_hours": round(busy / 3600.0, 3),
        # Eight decimals, not six: at six the resolution is a single watt-hour per
        # sample's worth of display, which is coarse enough to round a short
        # session's energy to a number that did not happen. The panel formats it;
        # this is the figure it formats.
        "kwh": round(kwh_num, 8) if readable else None,
        "kwh_busy": round(kwh_busy_num or 0.0, 8) if readable else None,
        "watts": round(float(watts), 1) if isinstance(watts, (int, float)) else None,
        "peak_watts": round(float(peak), 1) if isinstance(peak, (int, float)) else None,
        "samples": samples,
        "gaps": gaps,
        "since": since,
        "price": float(price or 0.0),
        "cost": cost,
        # What this is, said once and precisely, because a meter implies an
        # instrument and this is a sampled estimate from a running total that only
        # grows while Hermes is open.
        "measured": ("counted only while Hermes was running and watching the card, from "
                     "the power it reported at each sample — an estimate, not a meter"),
    }
    if samples == 0:
        out["note"] = "nothing sampled yet — this fills in while Hermes is open"
    elif seconds <= 0:
        out["note"] = "no time counted yet"
    elif not readable:
        out["note"] = ("this card does not report power draw, so energy and cost are "
                       "left empty rather than reported as zero")
    elif not price:
        out["note"] = "set a price per kWh to see this in money"
    return out


def attach(system_mod: Any, home_fn: Any) -> bool:
    """Hand this record every sample the monitor takes, once.

    Called by `plugin_api` at import. The sampler is the only place a reading of
    the card exists, and it must not be made to know about a cost total — so it
    offers `on_sample` and this subscribes. Idempotent by `_ATTACHED`, shared
    with every caller through the one module object.
    """
    global _ATTACHED
    if _ATTACHED:
        return False
    _ATTACHED = True
    system_mod.on_sample(lambda m: note(home_fn(), m))
    return True


def set_price(home: Path, value: float) -> Dict[str, Any]:
    """Where the price lives: beside the record, not in Athena's settings.

    Settings belong to the HTTP layer; this is a figure about the machine's own
    running total, and the summary route is the thing that needs both.
    """
    with _LOCK:
        st = _state(home)
        st["price"] = max(0.0, float(value or 0.0))
        _flush(home, force=True)
    return summary(home, price=float(value or 0.0))


def get_price(home: Path) -> float:
    with _LOCK:
        st = _state(home)
        try:
            return max(0.0, float(st.get("price") or 0.0))
        except Exception:  # noqa: BLE001
            return 0.0


def reset(home: Path) -> Dict[str, Any]:
    """Start the record over. Kept as an action because a total nobody can clear
    is a total nobody can act on."""
    with _LOCK:
        price = get_price(home)
        global _STATE
        _STATE = _blank()
        _STATE["_home"] = str(Path(home).resolve())
        _STATE["_last_mono"] = None
        _STATE["_flushed"] = 0.0
        _STATE["price"] = price
        _flush(home, force=True)
    return summary(home, price=price)


