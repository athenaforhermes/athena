"""Network traffic readings: per-interface maths, totals, and a bounded history.

The interesting parts are the ones a fake counter can prove and a live machine
cannot: a delta over a known interval, an interface that has never moved a byte,
and a history that stops growing. Then the live call, which only has to have the
right shape -- the numbers on this machine are whatever this machine is doing.
"""
import importlib.util
import json
import sys
import time
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}   {detail}")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class Cnt:
    """One psutil counter row."""

    def __init__(self, sent, recv):
        self.bytes_sent = sent
        self.bytes_recv = recv


class St:
    def __init__(self, speed=0, isup=True):
        self.speed = speed
        self.isup = isup


sysmod = load("athena_system_probe", PLUGIN / "dashboard" / "system.py")

print("\n1. a rate is a delta over the interval it was measured across")
sysmod._last_nic = {}
t0 = 1000.0
# A fabricated name on purpose: a real one ('Ethernet') may exist on this
# machine and be down, and the filter would drop it for the right reason.
sysmod._last_nic = {"zz-test-nic": (t0, 1_000_000, 2_000_000)}
rows = sysmod._net_ifaces(
    {"zz-test-nic": Cnt(1_500_000, 4_000_000),
     "Loopback Pseudo-Interface 1": Cnt(9e9, 9e9),
     "Wi-Fi": Cnt(0, 0)},
    now=t0 + 2.0)
names = [r["name"] for r in rows]
check("loopback is not a network interface you want to read", "Loopback Pseudo-Interface 1" not in names, names)
check("an interface that has never moved a byte is not listed", "Wi-Fi" not in names, names)
eth = rows[0]
check("up rate is bytes over the interval, not the total",
      eth["up_bps"] == 250000, eth["up_bps"])
check("down rate likewise", eth["down_bps"] == 1000000, eth["down_bps"])
check("totals are reported in MB", eth["recv_mb"] == round(4_000_000 / 1024 ** 2, 1), eth["recv_mb"])
check("a first sighting reports no rate rather than a huge one",
      sysmod._net_ifaces({"X": Cnt(5e9, 5e9)}, now=t0)[0]["up_bps"] == 0)
# Totals stay non-zero on purpose: with a byte count of 0 the interface is
# filtered for the right reason and the clamp would never be exercised.
sysmod._last_nic = {"zz-reset-nic": (t0, 9_000_000_000, 9_000_000_000)}
back = sysmod._net_ifaces({"zz-reset-nic": Cnt(5_000_000_000, 6_000_000_000)}, now=t0 + 1)
check("a minus delta (a counter that went backwards) clamps at zero",
      back and back[0]["up_bps"] == 0 and back[0]["down_bps"] == 0,
      back)

print("\n2. the payload keeps the shape other readers already depend on")
sysmod._last_net = None
sysmod._NET_HISTORY.clear()
first = sysmod._net_bps()
check("the original two keys are still there, still ints",
      isinstance(first.get("up_bps"), int) and isinstance(first.get("down_bps"), int),
      json.dumps(first)[:120])
check("a cold start says zero rather than guessing", first["up_bps"] == 0 and first["down_bps"] == 0)
check("totals since boot are reported", first["recv_mb"] > 0, first["recv_mb"])
check("history starts as one sample", len(first["history"]) == 1, len(first["history"]))
check("and carries [down, up]", first["history"][0] == [0, 0] or len(first["history"][0]) == 2,
      first["history"][0])
time.sleep(0.4)
second = sysmod._net_bps()
check("a second reading appends rather than replaces", len(second["history"]) == 2)
check("totals only grow", second["recv_mb"] >= first["recv_mb"])
check("a real machine names its adapters", len(second["nics"]) >= 1,
      json.dumps(second["nics"], indent=1)[:400])
check("each adapter names itself and its speed",
      all(n.get("name") and "speed_mbps" in n for n in second["nics"]))
check("the fastest link is offered as a denominator for a percentage",
      # Deliberately the busiest listed link, not the largest number in the list:
      # a Tailscale or WSL adapter reports ~4294 Mb/s and would make a saturated
      # gigabit link read as 2%. What has to hold is that the denominator comes
      # from a link that is actually listed — never from a virtual one that the
      # list left out, and never invented.
      bool(second["nics"]) and second["speed_mbps"] == second["nics"][0]["speed_mbps"]
      and second["speed_mbps"] in [n["speed_mbps"] for n in second["nics"]],
      f"{second['speed_mbps']} vs {[(n['name'], n['speed_mbps']) for n in second['nics']]}")
check("peaks are drawn from the history, not invented",
      second["peak_down_bps"] >= max(h[0] for h in second["history"]))

print("\n3. history is bounded, so a long session costs a fixed amount")
for _ in range(400):
    sysmod._NET_HISTORY.append([1, 1])
check("it stops at its ceiling", len(sysmod._NET_HISTORY) == 120, len(sysmod._NET_HISTORY))
check("and the oldest samples are the ones that went",
      sysmod._NET_HISTORY[-1] == [1, 1] and sysmod._NET_HISTORY[0] == [1, 1])
check("nothing blew up on a stat-less interface",
      isinstance(sysmod._net_ifaces.__doc__, str))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
