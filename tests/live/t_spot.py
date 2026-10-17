#!/usr/bin/env python3
"""The working arc: what an agent says it is touching, and whether the panel agrees.

Hermes Desktop draws a travelling arc around a sidebar row while that session is working.
Athena wears the same arc on the card an agent is using, which only works if two halves
that never call each other agree on a name: the agent broadcasts `app:wan`, and the card
carries `data-athena-spot="app:wan app:Wan Studio"`. Nothing type-checks that pair, and the
way it fails is silent — the arc simply never appears, which looks identical to "the agent
is not doing anything".

So this file holds both ends to the same contract:

  * `_spot_key` is pure and total, asserted case by case. It runs *before* every tool, so
    a key it cannot build must be an empty string, not an exception: a raise here is a tool
    that cannot run at all, for a cosmetic reason.
  * `_spot` must swallow a broken broadcast. The arc is how somebody watches an agent work;
    a tool that failed because the *watching* failed is a worse trade than no arc.
  * the three card kinds in the panel each carry the attribute the key is matched against,
    and the panel listens for the event the agent sends. A card that loses the attribute
    is an arc that never draws.

The mapping itself is data (`_SPOT_SUBJECT`/`_SPOT_ARGS`) so a new tool is a line, not a
second implementation to keep in step.

Run with `env -u PYTHONPATH` from this directory.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")
PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")
SRC = PLUGIN / "desktop" / "plugin.js.pre-merge"

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  -- {detail}" if detail else ""))


# ---- the agent half, loaded by path -----------------------------------------------------------
# `__init__.py` imports nothing but the standard library at module scope (the engine is
# loaded lazily), so it can be imported here without standing up Hermes.
spec = importlib.util.spec_from_file_location("athena_agent_spot", PLUGIN / "__init__.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

key = mod._spot_key

# Every tool that acts on something the panel draws a card for.
check("a launch names the app's card",
      key("launch", {"app": "wan"}) == "app:wan", repr(key("launch", {"app": "wan"})))
check("the `id` alias reaches the same card as `app`",
      key("stop", {"id": "wan"}) == "app:wan", repr(key("stop", {"id": "wan"})))
check("an install names the store card by its slug — the `install_with` handle",
      key("install", {"url": "chatbot-ollama"}) == "store:chatbot-ollama",
      repr(key("install", {"url": "chatbot-ollama"})))
check("a machine call names the machine's card",
      key("machines", {"op": "start", "name": "OmarchyVM"}) == "machine:OmarchyVM",
      repr(key("machines", {"name": "OmarchyVM"})))

# The negative half. A key of `app:` names nothing, and a selector for nothing matches the
# first card with the attribute rather than none — so the empty answer has to be empty.
check("a tool with no card behind it says nothing",
      key("status", {}) == "" and key("report", {}) == "",
      repr(key("status", {})))
check("a call that never said which app says nothing, rather than naming them all",
      key("launch", {}) == "" and key("machines", {"op": "list"}) == "",
      repr(key("launch", {})))
check("the name is trimmed, because it is about to be matched against an attribute",
      key("launch", {"app": "  wan  "}) == "app:wan", repr(key("launch", {"app": "  wan  "})))
check("a key that is only whitespace is the empty answer",
      key("launch", {"app": "   "}) == "" and key("install", {"url": ""}) == "",
      repr(key("launch", {"app": "   "})))

# Total, not just correct: this runs before every tool call, so `None` params and odd types
# have to come back as an empty answer rather than ending a tool that was fine. A name of
# `0` or `None` is nothing named — there is no machine called "0" — so the empty answer is
# the right one, and the point is that it is an answer at all.
check("nothing here can raise on the way to a key",
      key("launch", None) == "" and key("launch", {"app": None}) == ""
      and key("machines", {"name": 0}) == "",
      "a None/odd argument must be an empty key, never an exception")

# The broadcast is best-effort by design. Out here there is no TUI gateway, so the event is
# dropped on the floor by `plugin_events` itself — the requirement is that `_spot` returns
# quietly either way, and never takes the tool down with it.
try:
    out = mod._spot("launch", {"app": "wan"})
    raised = None
except Exception as exc:  # noqa: BLE001
    out, raised = None, repr(exc)
check("a broadcast that cannot happen does not fail the tool",
      raised is None and out is None, str(raised))

# ---- the panel half --------------------------------------------------------------------------
# The other end of the same contract, read from the file the panel is generated from: a fix
# in the generated `plugin.js` disappears on the next merge.
src = SRC.read_text(encoding="utf-8", errors="replace")

CARDS = {
    "an app's card answers to its id and its name": "'data-athena-spot': 'app:' + app.id",
    "a machine's card answers to its name": "'data-athena-spot': 'machine:' + m.name",
    "a store card answers to its slug and its name": "'data-athena-spot': 'store:' + app.slug",
}
for what, needle in CARDS.items():
    check(what, needle in src, needle)

check("the panel listens for the event the agent sends",
      "host.onEvent('plugin.athena.spot'" in src, "no spot subscription")
check("the ring is drawn and torn down on a timer",
      "function spotArc(" in src and "clearTimeout(had.timer)" in src
      and "spotRings.delete(el)" in src, "spotArc is missing or leaks its ring")

# Only one tab draws each kind of card, so most of the time the card an agent named is not
# mounted at all — and a spotlight for an unmounted card is no spotlight. The panel follows the
# agent to the tab that draws it, which is what `screen.wanted` already does for a machine.
check("the panel follows the agent to the tab that draws the card it named",
      "function spotFollow(" in src
      and "SPOT_TABS = { app: 'apps', store: 'store', machine: 'machines' }" in src
      and "spotFollow(p.key, p.ms, (t) => tab.set(t))" in src,
      "the card is off screen and nothing follows it there")
check("... and that wait is bounded, so a card that never appears gives up quietly",
      "let left = 12" in src and "--left > 0" in src,
      "a retry that could outlive the tab it is waiting for")
check("the panel stylesheet carries the arc Hermes draws",
      "@keyframes athena-arc" in src and ".athena-arc::before" in src
      and "240%" in src, "the ported animation is missing")
check("the arc's host is a containing block, or the ring anchors to the pane",
      "gap:7px; background:var(--card); position:relative;" in src,
      "`.ah-card` must be positioned for the arc to sit on the card")

# ---- and the harness can reach the pure half --------------------------------------------------
mk = (PLUGIN / "tests" / "live" / "mk_ui_test.py").read_text(encoding="utf-8")
check("the two pure halves are exported to the UI harness",
      '"spotSelector"' in mk and '"spotTab"' in mk, "spotSelector/spotTab is not in EXPORTS")

print(f"\n{PASS} ok, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
