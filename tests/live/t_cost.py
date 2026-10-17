"""What this machine has spent on its card: the size of the total, and its honesty.

The cost record is a running total folded together from the monitor's own samples —
the time the card was working, and the energy it drew while Hermes was open. There
is no meter to read here, so almost everything worth checking is about what the
numbers are *allowed* to claim:

  * the arithmetic: a watt-second is not a watt-hour, and a kilowatt-hour is not
    three thousand six hundred watt-seconds (each of the two divisor mistakes is
    checked below, because both look right);
  * what is *not* credited: a gap in sampling is unknown power, not idle power,
    and a backwards clock contributes nothing rather than a negative;
  * what is *not* claimed: a card with no power sensor gets a null, not a zero; a
    kWh with no price gets a null cost, not "$0.00";
  * one copy of the record: the loader hands the same module to both halves of the
    plugin, and the monitor is subscribed exactly once — a second subscription
    would fold every sample in twice, which no reader could see.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")

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


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


api = load("athena_api_under_test", PLUGIN / "dashboard" / "plugin_api.py")

# --------------------------------------------------------------- one record
cost = api._cost_module()
check("the record loads through plugin_api", cost is not None)
if cost is None:
    print("t_cost: 0 passed, 1 failed")
    sys.exit(1)

check("and the door hands back the same module every time it is opened, so the "
      "router's copy and the tools' copy cannot hold two different totals",
      cost is api._cost_module() and sys.modules.get("athena_cost") is cost)

system_mod = api.athena_system
check("the monitor came with the plugin, so there is something to subscribe to",
      system_mod is not None and hasattr(system_mod, "on_sample"))
if system_mod is None:
    print("t_cost: %d passed, 1 failed" % PASS)
    sys.exit(1)

hooks_before = len(system_mod._SAMPLE_HOOKS)
check("the monitor was handed to the cost record exactly once at import",
      hooks_before == 1, f"{hooks_before} hook(s): {system_mod._SAMPLE_HOOKS}")
# The second copy of plugin_api in this process (the agent tools' own load) runs
# the same registration. It must be a no-op: two hooks means every sample counted
# twice, a total that is wrong by exactly the factor nobody can see.
check("subscribing a second time is refused rather than added",
      cost.attach(system_mod, api._athena_home) is False
      and len(system_mod._SAMPLE_HOOKS) == hooks_before,
      str(system_mod._SAMPLE_HOOKS))

# --------------------------------------------------- what the numbers are worth
tmp = Path(tempfile.mkdtemp(prefix="athena-cost-"))
h1, h2, h3, h4 = tmp / "one", tmp / "two", tmp / "three", tmp / "four"

BUSY = {"util": 90.0, "busy": True, "power_w": 300.0}
IDLE = {"util": 0.0, "busy": False, "power_w": 25.0}

print("\n1. the arithmetic, against the two divisors that both look right")
cost.note(h1, BUSY, now_mono=1000.0)          # anchors: nothing credited yet
s0 = cost.summary(h1)
check("the first sample anchors the clock and credits no time",
      s0["seconds"] == 0.0 and s0["samples"] == 1, json.dumps(s0)[:200])
for i, t in enumerate((1003.0, 1006.0, 1009.0), start=1):
    cost.note(h1, BUSY, now_mono=t)
s = cost.summary(h1)
check("three 3-second samples are nine seconds of the card working",
      s["seconds"] == 9.0 and s["busy_seconds"] == 9.0, json.dumps(s)[:220])
# 300 W for 9 s is 2700 W·s = 0.00075 kWh. As watt-hours it would be 0.75, and the
# cost below would be a thousand times out.
check("and 300 W held for 9 s is 0.00075 kWh, not 0.75",
      abs((s["kwh"] or 0) - 0.00075) < 1e-9, str(s["kwh"]))
check("a working card counts as busy time and energy",
      s["kwh"] == s["kwh_busy"], f"{s['kwh']} vs {s['kwh_busy']}")
check("the hours are derived from the seconds rather than counted separately",
      abs(s["hours"] - s["seconds"] / 3600.0) < 0.001
      and abs(s["busy_hours"] - s["busy_seconds"] / 3600.0) < 0.001,
      f"{s['hours']} / {s['busy_hours']}")
check("the last and peak readings are kept for the panel to show",
      s["watts"] == 300.0 and s["peak_watts"] == 300.0, json.dumps(s)[:200])

print("\n2. an idle card: still time, still power, not 'busy'")
cost.note(h2, BUSY, now_mono=2000.0)
for t in (2003.0, 2006.0):
    cost.note(h2, IDLE, now_mono=t)
s2 = cost.summary(h2)
check("the time is counted either way", s2["seconds"] == 6.0, str(s2["seconds"]))
check("but only the working part is called busy",
      s2["busy_seconds"] == 0.0 and s2["busy_hours"] == 0.0, str(s2["busy_seconds"]))
# 25 W idle for 6 s = 150 W·s = 0.0000417 kWh. A card holding a loaded model still
# draws; leaving it out would understate the electricity and overstate the card.
check("an idle card still draws, and that is counted as energy",
      abs((s2["kwh"] or 0) - 150.0 / 3600000.0) < 1e-8, str(s2["kwh"]))
check("while the energy attributed to working stays at zero",
      s2["kwh_busy"] == 0.0, str(s2["kwh_busy"]))
check("and the peak remembers the busiest reading seen, not the last",
      s2["peak_watts"] == 300.0 and s2["watts"] == 25.0,
      f"peak={s2['peak_watts']} last={s2['watts']}")

print("\n3. what is deliberately not credited")
cost.note(h2, BUSY, now_mono=2060.0)          # a 54-second gap in sampling
s3 = cost.summary(h2)
check("a gap in sampling credits no time at all",
      s3["seconds"] == 6.0, f"{s3['seconds']}s after a 54s gap")
check("and no energy either — the power over a gap is unknown, not idle",
      abs((s3["kwh"] or 0) - 150.0 / 3600000.0) < 1e-8, str(s3["kwh"]))
check("the gap is counted, so 'why is this total small' has an answer",
      s3["gaps"] == 1, str(s3["gaps"]))
cost.note(h2, BUSY, now_mono=2063.0)
s3b = cost.summary(h2)
check("the sample right after a gap resumes the record rather than anchoring again",
      s3b["seconds"] == 9.0 and s3b["gaps"] == 1,
      f"{s3b['seconds']}s, {s3b['gaps']} gap(s)")
cost.note(h2, BUSY, now_mono=2060.0)          # a clock that went backwards
s3c = cost.summary(h2)
check("a backwards clock contributes nothing rather than a negative",
      s3c["seconds"] == 9.0 and (s3c["kwh"] or 0) >= (s3b["kwh"] or 0),
      f"{s3c['seconds']}s")
cost.note(h2, BUSY, now_mono=2063.0)
check("and the sample after it re-anchors and keeps counting",
      cost.summary(h2)["seconds"] == 12.0, str(cost.summary(h2)["seconds"]))
# The boundary itself: a step of exactly one interval-plus is a gap, and the last
# honest step is credited. Off by one here would silently drop every slow sample.
h5 = tmp / "five"
cost.note(h5, BUSY, now_mono=3000.0)
cost.note(h5, BUSY, now_mono=3008.0)
check("a step of exactly the longest allowed is credited",
      cost.summary(h5)["seconds"] == 8.0, str(cost.summary(h5)["seconds"]))
cost.note(h5, BUSY, now_mono=3016.1)
check("and a step just past it is a gap rather than credited time",
      cost.summary(h5)["seconds"] == 8.0 and cost.summary(h5)["gaps"] == 1,
      str(cost.summary(h5)))

print("\n4. what it refuses to claim")
cost.note(h3, {"util": 50.0, "busy": True}, now_mono=4000.0)
cost.note(h3, {"util": 50.0, "busy": True}, now_mono=4003.0)
s4 = cost.summary(h3)
check("a card that reports no wattage gets time counted",
      s4["seconds"] == 3.0, str(s4["seconds"]))
check("and a null for the energy, never a zero standing in for it",
      s4["kwh"] is None and s4["kwh_busy"] is None, json.dumps(s4)[:200])
check("with the reason said out loud rather than left to be guessed",
      "does not report power draw" in (s4.get("note") or ""), str(s4.get("note")))
check("and no money is invented from an energy nobody measured",
      s4["cost"] is None, str(s4["cost"]))
check("a metrics dict with nothing recognisable in it changes nothing",
      (cost.note(h3, {}, now_mono=4006.0) is None
       and cost.summary(h3)["seconds"] == 3.0), str(cost.summary(h3)["seconds"]))

print("\n5. the price is the user's to set, and its absence is not a zero")
# 600 s of the card working, in steps the record will credit: one long step would
# be a gap, which is the point of the check two sections up.
cost.note(h4, BUSY, now_mono=5000.0)
for _i in range(1, 201):
    cost.note(h4, BUSY, now_mono=5000.0 + 3.0 * _i)
no_price = cost.summary(h4)
check("with no price, the energy is a number and the money is absent",
      abs((no_price["kwh"] or 0) - 0.05) < 1e-6 and no_price["cost"] is None,
      json.dumps(no_price)[:220])
check("and the panel is told what to do about it",
      "set a price" in (no_price.get("note") or ""), str(no_price.get("note")))
priced = cost.set_price(h4, 0.21)
check("a price turns the same energy into money, and rounds to something readable",
      priced["price"] == 0.21 and priced["cost"] is not None
      and abs(priced["cost"] - 0.0105) < 1e-5,
      json.dumps(priced)[:220])
check("a price of zero is 'not set', not 'free'",
      cost.set_price(h4, 0.0)["cost"] is None, str(cost.set_price(h4, 0.0)["cost"]))
check("and it comes back as it was set",
      abs(cost.get_price(h4) - 0.0) < 1e-12, str(cost.get_price(h4)))

print("\n6. the record survives a restart, and a broken file is not fatal")
cost.set_price(h4, 0.21)                      # forces the pending total to disk
before = cost.summary(h4)
check("the summary knows the price even though the route will pass it too",
      cost.get_price(h4) == 0.21, str(cost.get_price(h4)))
cost.note(h5, BUSY, now_mono=6000.0)          # look at another home...
after = cost.summary(h4)                      # ...and come back to this one
check("coming back to a home re-reads its record rather than starting over",
      after["seconds"] == before["seconds"] and after["samples"] == before["samples"],
      f"{after['seconds']}s/{after['samples']} vs {before['seconds']}s/{before['samples']}")
check("including the energy and the price stored beside it",
      after["kwh"] == before["kwh"] and after["price"] == 0.21,
      json.dumps(after)[:220])
check("importantly it does not re-count the samples it just read back",
      cost.summary(h4)["samples"] == before["samples"], str(cost.summary(h4)["samples"]))

h6 = tmp / "six"
(h6 / "cache").mkdir(parents=True, exist_ok=True)
(h6 / "cache" / "gpu-cost.json").write_text("{not json at all", encoding="utf-8")
broken = cost.summary(h6)
check("a corrupt record reads as an empty one instead of raising",
      broken["ok"] is True and broken["samples"] == 0, json.dumps(broken)[:180])
check("and a fresh sample then writes a good file over it",
      cost.note(h6, BUSY, now_mono=7000.0) is None
      and cost.summary(h6)["samples"] == 1, str(cost.summary(h6)["samples"]))
h7 = tmp / "seven"
(h7 / "cache").mkdir(parents=True, exist_ok=True)
(h7 / "cache" / "gpu-cost.json").write_text("[1, 2, 3]", encoding="utf-8")
check("a record holding the wrong shape is treated the same way",
      cost.summary(h7)["samples"] == 0 and cost.summary(h7)["ok"] is True)

print("\n7. the route the panel asks")
_real_home = api._athena_home
api._athena_home = lambda: h4
try:
    r = api.cost_summary()
    body = json.loads(r.body.decode())
    check("it answers with the record", r.status_code == 200 and body["ok"] is True,
          r.body.decode()[:180])
    check("including the price the record holds, without being told again",
          body["price"] == 0.21 and body["cost"] is not None, json.dumps(body)[:200])
    check("and says once, precisely, what this is not",
          "not a meter" in body["measured"] and "Hermes was running" in body["measured"],
          str(body["measured"]))
    bad = api.cost_set_price(api.CostPriceBody(price=-1))
    check("a negative price is refused", bad.status_code == 400, bad.body.decode()[:140])
    silly = api.cost_set_price(api.CostPriceBody(price=5000))
    check("and a price a thousand times too big is refused rather than shown as a cost",
          silly.status_code == 400 and "1000" in silly.body.decode(),
          silly.body.decode()[:180])
    good = json.loads(api.cost_set_price(api.CostPriceBody(price=0.19)).body.decode())
    check("a sane price is stored and reflected at once",
          good["ok"] is True and good["price"] == 0.19 and good["cost"] is not None,
          json.dumps(good)[:200])
    reset = json.loads(api.cost_reset().body.decode())
    check("starting over clears the total", reset["seconds"] == 0.0
          and reset["samples"] == 0 and reset["kwh"] is None, json.dumps(reset)[:200])
    check("but keeps the price, which is a setting and not a reading",
          reset["price"] == 0.19, str(reset["price"]))
    check("and the cleared record is the one on disk, not just in memory",
          abs(cost.get_price(h4) - 0.19) < 1e-12, str(cost.get_price(h4)))
finally:
    api._athena_home = _real_home

print("\n8. the monitor's samples really arrive, once each")
real_home = api._athena_home()
was = cost.summary(real_home)["samples"]
for hook in list(system_mod._SAMPLE_HOOKS):
    hook(BUSY)
now = cost.summary(real_home)
check("one sample taken by the monitor is one sample in the record",
      now["samples"] == was + 1, f"{was} -> {now['samples']}")
check("and it was folded in once, not twice: one hook, one count",
      len(system_mod._SAMPLE_HOOKS) == 1, str(system_mod._SAMPLE_HOOKS))
# The sampler hands each sample to every hook inside a try/except, so a record
# that starts raising cannot take the monitor down with it. Checked against the
# source: driving the real loop for a second would need a real card, and this file
# is about decisions rather than about nvidia-smi being installed.
_src = (PLUGIN / "dashboard" / "system.py").read_text(encoding="utf-8")
_loop = _src[_src.find("def _sampler_loop"):]
_loop = _loop[:_loop.find("def _ensure_sampler")]
check("a hook that raises is caught, so record-keeping cannot stop the monitor",
      "for hook in list(_SAMPLE_HOOKS)" in _loop and "except Exception" in _loop
      and "hook(m)" in _loop, "the hook call is not guarded in _sampler_loop")

print()
print(f"t_cost: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("failed: " + "; ".join(FAILURES))
sys.exit(1 if FAIL else 0)
