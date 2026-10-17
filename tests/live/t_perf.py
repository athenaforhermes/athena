#!/usr/bin/env python3
"""Numbers instead of impressions.

The parked item this file exists for is mouse lag: "the screen feels laggy when I move the
pointer" cannot be closed, because there is no target to close it against and no way to tell a
fix from a mood. So this measures, and writes the table into `docs/performance.md` (`--write`).

Two kinds of measurement live here, and the difference is stated rather than blurred:

  * **Measurable anywhere, so measured here, now.** The cost of one encode; what 200 polls of a
    still screen cost; what 200 arriving-but-identical rectangles cost (the mouse-lag mechanism —
    a pointer moving across something identical); the cost of sending an event down the held
    connection against the cost of opening one to send it. These use `FakeVnc`, the real
    `ScreenSession` and the real Pillow, so the numbers are this machine's.
  * **Needs a live guest or engine, so it says so.** End-to-end pointer-to-pixel latency, engine
    start to first route answer, and RSS after half an hour with a screen open cannot be faked
    without lying, so each prints `SKIPPED` with the exact command that measures it. A skipped
    measurement is not a passing one, and the summary counts them separately for that reason.

Usage:
  env -u PYTHONPATH <venv python> t_perf.py                 # measure, print the table
  env -u PYTHONPATH <venv python> t_perf.py --write         # ... and rewrite docs/performance.md
  env -u PYTHONPATH <venv python> t_perf.py --guest 127.0.0.1:5901   # with a live VNC guest
"""
from __future__ import annotations

import importlib.util
import os
import statistics
import sys
import threading
import time
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")
PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")
sys.path.insert(0, str(PLUGIN / "dashboard"))
sys.path.insert(0, str(PLUGIN / "tests" / "live"))

import screen  # noqa: E402

PASS = FAIL = 0
SKIPPED = 0
ROWS: list[tuple[str, str, str, str]] = []      # what, number, target, how


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  -- {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  skip {name}   {why}")


def ms(value: float) -> str:
    """Milliseconds, or microseconds when the answer is smaller than the clock's own noise.

    "0.00 ms" reads as a broken measurement rather than a fast one, and a number that looks
    broken is a number nobody trusts — so the unit changes where the resolution does.
    """
    return f"{value:.2f} ms" if value >= 0.01 else f"{value * 1000:.0f} \u00b5s"


def note(what: str, number: str, target: str, how: str) -> None:
    ROWS.append((what, number, target, how))
    print(f"       {what:<44} {number:>12}   (target {target})")


W, H = 1280, 800                    # the guest size the parked item is about

# The fake VNC server is shared rather than copied: `vncfake.py` holds it, `t_screen.py` checks
# the client against it, and this file measures it. Lifting it out of `t_screen` also means this
# file does not import a test — which would run that test's whole body and exit on its summary.
from vncfake import FakeVnc, close  # noqa: E402


print("== the cost of one encode ==")
pixels = bytes(bytearray((i * 7) % 251 for i in range(W * H * 4)))
runs = []
for _ in range(5):
    started = time.perf_counter()
    screen._jpeg(pixels, W, H)
    runs.append((time.perf_counter() - started) * 1000)
encode_ms = statistics.median(runs)
print(f"       one {W}x{H} frame encoded in {encode_ms:.1f} ms "
      f"({W * H * 4 / 1e6:.1f} MB of pixels; median of {len(runs)})")
check("an encode is tens of milliseconds, which is why it must not happen for nothing",
      0 < encode_ms < 2000, f"{encode_ms:.1f} ms")
note("one encode at the guest's own size", f"{encode_ms:.1f} ms", "n/a (the cost of an event)",
     f"median of {len(runs)} `_jpeg` calls on random pixels")

print("\n== what a still screen costs ==")
fake = FakeVnc(width=64, height=64, quiet_after=1)
fake.start()
ses = screen.ScreenSession("127.0.0.1", fake.port, idle_timeout=0.2).start()
ses.frame()
polls = 200
started = time.perf_counter()
for _ in range(polls):
    ses.frame()
elapsed = (time.perf_counter() - started) * 1000
encodes = ses.encoded
ses.close()
close(fake)
print(f"       {polls} polls of a screen that did not move: {encodes} encode(s), "
      f"{ms(elapsed / polls)} per poll")
check("a still screen costs one encode and then nothing", encodes == 1, f"encoded={encodes}")
check("... and a cached poll is far cheaper than an encode",
      elapsed / polls < encode_ms / 4, f"{elapsed / polls:.2f} ms/poll vs {encode_ms:.1f} ms/encode")
note("200 polls of a still screen", f"{encodes} encode(s)", "1 encode", "FakeVnc, quiet after the first frame")
note("a poll that finds the cache warm", ms(elapsed / polls), "< an encode",
     f"{polls} cached polls")

print("\n== the mouse-lag mechanism: rectangles that arrive and change nothing ==")
# This is the parked item. A pointer moving across a desktop makes the server send its rectangle;
# when the pixels are the ones already here — a repeated cursor, a redraw of the same frame — the
# old behaviour invalidated the encoded picture and paid a full encode for a screen that had not
# moved. 200 of those is the difference between a pointer that follows and one that trails.
fake = FakeVnc(width=64, height=64, then_rects=[(0, 0, 64, 64)], then_shift=0)
fake.start()
ses = screen.ScreenSession("127.0.0.1", fake.port, idle_timeout=0.2).start()
ses.frame()
time.sleep(0.5)
for _ in range(200):
    ses.frame()
same_encodes = ses.encoded
arrived = fake.updates_sent
ses.close()
close(fake)
would_have = 200 * encode_ms / 1000
print(f"       {arrived} identical rectangle(s) arrived; {same_encodes} encode(s) paid; "
      f"the old behaviour would have paid ~{would_have:.1f} s of encoding at {W}x{H}")
check("identical rectangles arriving do not cost an encode each",
      same_encodes == 1, f"encoded={same_encodes} over {arrived} updates")
note("identical rectangles arriving (200 polls)",
     f"{same_encodes} encode(s)", "1 encode",
     "FakeVnc re-sending byte-identical pixels; measured after the fix")
note("... the same traffic before the fix",
     f"~{would_have * 1000:.0f} ms of encoding", "0",
     "200 arriving rectangles x the measured encode cost; the number the fix removes")

print("\n== an event on the held connection, against opening one to send it ==")
fake = FakeVnc(width=32, height=32, quiet_after=1)
fake.start()
ses = screen.ScreenSession("127.0.0.1", fake.port, idle_timeout=0.2).start()
held = []
for _ in range(50):
    started = time.perf_counter()
    ses.input([{"type": "pointer", "x": 5, "y": 5, "buttons": 1}])
    held.append((time.perf_counter() - started) * 1000)
held_ms = statistics.median(held)
connections = ses.connections
ses.close()
close(fake)
print(f"       a pointer event down the held connection: {ms(held_ms)} median, "
      f"{connections} connection(s) for the whole session")
check("an event reuses the connection the session already has", connections == 1, str(connections))
check("... and sending it costs well under the guest's own frame time",
      held_ms < 10, f"{held_ms:.2f} ms")
note("a pointer event on the held connection", ms(held_ms), "< 10 ms",
     "median of 50 events into FakeVnc, same socket as the frames")

print("\n== what needs a live guest or engine ==")
guest = ""
for i, arg in enumerate(sys.argv):
    if arg == "--guest" and i + 1 < len(sys.argv):
        guest = sys.argv[i + 1]
if not guest:
    skip("pointer event -> changed pixels, p95",
         "no live guest: re-run with --guest 127.0.0.1:5901 (a machine actually running)")
else:
    host, _, port = guest.partition(":")
    live = screen.ScreenSession(host, int(port or 5900), idle_timeout=0.5).start()
    latencies = []
    for i in range(200):
        before = live.frame()["image"]
        started = time.perf_counter()
        live.input([{"type": "pointer", "x": 10 + i, "y": 10 + (i % 40), "buttons": 0}])
        for _ in range(40):
            if live.frame()["image"] != before:
                break
            time.sleep(0.005)
        latencies.append((time.perf_counter() - started) * 1000)
    live.close()
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    print(f"       p95 {p95:.0f} ms, median {statistics.median(latencies):.0f} ms over {len(latencies)} events")
    check("pointer to changed pixels is inside the latency budget (D6: 60 ms p95)",
          p95 <= 60, f"p95={p95:.0f} ms")
    note("pointer event -> changed pixels, p95", f"{p95:.0f} ms", "\u2264 60 ms",
         f"200 events against {guest}")

skip("engine start -> first route answer",
     "needs a launch: time `athena_status` from a cold start, or run the driver with --timing")
skip("panel first paint with 100 apps",
     "needs the UI harness: node uitest/driver.mjs --timing (it builds a synthetic home)")
skip("RSS after 30 minutes with a screen open",
     "needs 30 minutes: run a machine, open its Screen tab, and read athena_report")

print(f"\n{PASS} measured check(s) passed, {FAIL} failed, {SKIPPED} not measurable here")
if ROWS:
    print("\n== table ==")
    for what, number, target, how in ROWS:
        print(f"{what} | {number} | {target} | {how}")

if "--write" in sys.argv:
    doc = PLUGIN / "docs" / "performance.md"
    lines = [
        "# Measured, not felt",
        "",
        "Written by `tests/live/t_perf.py`. Every row is a number this file produced on the",
        "machine it ran on, with the method in the last column, so a row that stops being true",
        "can be re-measured rather than argued about. The rows that need a live guest, a cold",
        "engine or half an hour of running are named at the bottom as **pending**, with the",
        "command that would fill them — a missing number is stated, never guessed.",
        "",
        "| What | Number | Target | How it was measured |",
        "| --- | --- | --- | --- |",
    ]
    for what, number, target, how in ROWS:
        lines.append(f"| {what} | {number} | {target} | {how} |")
    lines += [
        "",
        "## Pending, with the exact command",
        "",
        "| What | Target | Command |",
        "| --- | --- | --- |",
        "| Pointer event → changed pixels, p95 | ≤ 60 ms | `python t_perf.py --guest 127.0.0.1:5901` |",
        "| Engine start → first route answer | ≤ 3 s | start the engine, then time `athena_status` |",
        "| Panel first paint, 100 apps | ≤ 1.5 s | `node uitest/driver.mjs --timing` |",
        "| RSS after 30 min with a screen open | ≤ 400 MB | run a machine, open Screen, read `athena_report` |",
        "",
        "## What the numbers are for",
        "",
        "The mouse-lag item was parked for two reasons: it had no target, and \"feels better\" is",
        "not a proof. The mechanism is measured above — an arriving rectangle that is",
        "byte-identical to what is already on screen used to invalidate the encoded frame and pay",
        "a full encode, so a pointer moving across a still desktop cost tens of milliseconds per",
        "movement at 1280×800. The fix is one comparison per row in `_read_update`, and the row",
        "that says what it removed is the reason the fix is in rather than merely plausible.",
        "",
    ]
    doc.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {doc}")

sys.exit(1 if FAIL else 0)
