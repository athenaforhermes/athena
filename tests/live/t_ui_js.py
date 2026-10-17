#!/usr/bin/env python3
"""Every failure in the panel says something, and the rule is a check rather than a habit.

The backend answers **200 with `{ok: false}`** when it refuses — that is the honest error taxonomy
this project settled on early, because "the port is taken" and "the request was malformed" are
different things and a status code cannot say both. The cost of that choice is that a panel which
reads only the status code sees success: the button flashes, the card goes back to "stopped", and
nothing explains. So the panel's own rule is that a call's answer is inspected before it is
believed.

Two halves, because either one alone can be satisfied while the bug survives:

  * **Static** — every *mutating* call site (`POST`/`DELETE`/`PUT`/`PATCH` through `ctx.rest`) must
    check the answer inside its own block, or be listed below with a reason. Reads that render
    whatever came back are not in scope: a card that draws the answer cannot hide a refusal. This
    is the same triage shape as the theme file's status allow list — adding a call site is a
    decision, not a default.
  * **Behavioural** — the function that does the checking is *run*, not read: a refusal with a
    reason, a refusal without one, a success, a `null`, and a string all have to produce the
    sentence the panel would show. `actionFailure` is lifted out of the panel source and executed
    by Node, so what is asserted is the answer rather than the shape of the code.

An audit of this file found nine mutating sites that treated a refusal as success — including the
screen's own input route, whose `ok: false` (`human_has_control`) was the control lease refusing a
keystroke. That one is asserted by name below, because it is the case where a silent failure means
somebody typed into a machine and nothing happened.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent.parent
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


# Live mutating call sites that are allowed to skip the check, each with its reason. Empty on
# purpose: every site the audit found has been fixed, so a new entry here is a decision somebody
# has to write down. An entry is the route literal plus a reason, matched by route.
UNCHEKED_OK: dict[str, str] = {}

OKCHECK = re.compile(r"\.ok\b|actionFailure\(")

if not SRC.exists():
    print(f"the panel's source is not here: {SRC}")
    sys.exit(1)
text = SRC.read_text(encoding="utf-8", errors="replace")
lines = text.splitlines()

# ---- 1. the static rule -----------------------------------------------------------------------
unchecked: list[str] = []
for i, line in enumerate(lines):
    if "await ctx.rest(" not in line:
        continue
    window = "\n".join(lines[i:i + 10])
    if not re.search(r"method:\s*'(POST|DELETE|PUT|PATCH)'", window):
        continue                                  # a read that renders its answer is not in scope
    m = re.search(r"ctx\.rest\(\s*([`'\"][^`'\"]*[`'\"])",
                  line + " " + (lines[i + 1] if i + 1 < len(lines) else ""))
    route = (m.group(1) if m else "?")
    key = route.strip("`'\"")
    if OKCHECK.search(window) or key in UNCHEKED_OK:
        continue
    unchecked.append(f"{i + 1}: {route}")

check("every call that changes something reads the answer before believing it",
      not unchecked, "; ".join(unchecked[:5]) or "")
check("... and the scan found the call sites, so an empty answer means something",
      sum(1 for line in lines if "await ctx.rest(" in line) > 50,
      f"{sum(1 for line in lines if 'await ctx.rest(' in line)} call site(s)")

# ---- 2. the lease's refusal is surfaced by name -----------------------------------------------
# The screen's input route is the one place a person meets the control lease: an agent's keystroke
# comes back `ok: false` with `human_has_control`, and the panel has to say so. Asserted by name
# because the mechanism is the point, not the presence of a helper.
check("the screen's own input route hands a refusal to the panel, by name",
      "actionFailure('That keystroke'" in text)

# ---- 3. the checking function, run rather than read ---------------------------------------------
# Lifted out of the source and executed: what matters is the sentence, not the syntax.
m = re.search(r"function actionFailure\([\s\S]*?\n\}", text)
check("`actionFailure` is in the panel source and still says `ok !== false`",
      bool(m) and "res.ok !== false" in m.group(0))
if m:
    node = os.environ.get("ATHENA_NODE") or "node"
    script = (
        m.group(0) + "\n"
        "const cases = [\n"
        "  { label: 'a refusal with a reason', res: { ok: false, error: 'the port is taken' } },\n"
        "  { label: 'a refusal with a detail and no error', res: { ok: false, detail: 'no free card' } },\n"
        "  { label: 'a refusal with neither', res: { ok: false } },\n"
        "  { label: 'a success', res: { ok: true } },\n"
        "  { label: 'a success with no ok field, which is not a refusal', res: { url: 'http://x' } },\n"
        "  { label: 'nothing at all', res: null },\n"
        "  { label: 'a string where an object was expected', res: 'nope' },\n"
        "];\n"
        "console.log(JSON.stringify(cases.map(c => [c.label, actionFailure('Doing that', c.res)])));\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        tmp = fh.name
    try:
        proc = subprocess.run([node, tmp], capture_output=True, text=True, timeout=60)
        answers = dict(json.loads(proc.stdout.strip().splitlines()[-1])) if proc.returncode == 0 else {}
    except Exception as e:                                     # noqa: BLE001
        print(f"  note  node could not run the function: {e}")
        answers = {}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    check("a refusal is turned into a sentence naming what was attempted and why",
          answers.get("a refusal with a reason") == "Doing that: the port is taken",
          str(answers.get("a refusal with a reason")))
    check("... and a refusal that brought a detail instead of an error is still explained",
          answers.get("a refusal with a detail and no error") == "Doing that: no free card",
          str(answers.get("a refusal with a detail and no error")))
    check("... and a refusal with no words at all still says the attempt failed",
          answers.get("a refusal with neither") == "Doing that: failed",
          str(answers.get("a refusal with neither")))
    check("a success, an answer with no `ok` field, a null and a string are all silent",
          all(answers.get(k) == "" for k in (
              "a success", "a success with no ok field, which is not a refusal",
              "nothing at all", "a string where an object was expected")),
          str(answers))

print(f"\n{PASS} ok, {FAIL} failed")
sys.exit(1 if FAIL else 0)
