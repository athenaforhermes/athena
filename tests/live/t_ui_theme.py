#!/usr/bin/env python3
"""The panel on somebody else's theme.

Athena's panel is injected into a frame the app owns, and the app can be dark or light. A colour
written straight into the panel's CSS is a colour chosen for one theme and shipped to both, which
is how a light-theme user ends up with a black rectangle where the screen viewer should be — the
bug this file exists to catch at write time rather than in a screenshot later.

The rule, and its two honest exceptions:

  * **A surface is a theme variable.** `--card`, `--border`, `--foreground`, `--muted-foreground`,
    `--accent` — the five the frame injects before the panel's own styles run.
  * **A status colour may be a literal.** Green means healthy, red means broken, amber means
    careful: those are *meanings*, not surfaces, and a theme that re-tinted them would be lying
    about a state. They are listed below, so adding one is a decision rather than a typo.
  * **A literal may be a fallback for a theme variable** — `var(--card, #0b0b0f)` — because that
    is the theme variable with a stated last resort, and the last resort is what renders when the
    frame injects nothing (a bare test harness, an older app).

Everything is checked against `desktop/plugin.js.pre-merge`, the file the panel is actually
generated from: a fix applied to the generated `plugin.js` is a fix that disappears on the next
merge, and `run_all.sh`'s `build committed` step proves the two agree (rule 144 in the skill).
"""
from __future__ import annotations

import re
import sys
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


# ---- what is allowed to be a literal, and why ------------------------------------------------

# Status colours. Each one means something; none of them is a surface.
STATUS = {
    "#22c55e": "healthy / running",
    "#3fb950": "healthy (GitHub's green, used beside it)",
    "#22c55e66": "healthy, washed — a background tint",
    "#22c55e88": "healthy, washed — a stronger tint",
    "#ef4444": "broken / refused",
    "#f85149": "broken (GitHub's red, used beside it)",
    "#f59e0b": "careful / degraded",
    "#f59e0b66": "careful, washed — a background tint",
    "#eab308": "careful / stale",
    "#d29922": "careful (GitHub's amber)",
    "#94a3b8": "a muted ink for text drawn over a picture, where the app's own ink cannot reach",
}

# The five the frame injects, plus the ones the app defines for plugins generally.
KNOWN_VARS = {"--card", "--border", "--foreground", "--muted-foreground", "--accent",
              "--background", "--accent-foreground", "--popover", "--muted"}

# Variables the panel declares for ITSELF, inside its own stylesheet — the working arc's
# geometry and colours. A name in here is allowed to lead a `var()` only because check 2c
# proves it is actually declared, so this set cannot smuggle in a name with nothing behind
# it: that is the bug the check above exists to catch.
PANEL_VARS = {"--arc-c1", "--arc-c2", "--arc-angle", "--arc-width", "--arc-standoff",
              "--arc-radius", "--arc-duration"}

# Names the retired standalone filebox and sysmon plugins used, kept only as *fallbacks* inside a
# variable that this app does inject. On their own they resolve to nothing: `color` inherits by
# accident, `background` disappears, and a progress bar drawn with one is invisible — which is
# exactly what an audit of this file found (the memory meter's fill had no colour at all). A name
# in this set is allowed to appear on the right-hand side of a comma, never as the primary.
FALLBACK_NAMES = {"--ui-text-primary", "--ui-bg-secondary", "--ui-bg-editor",
                  "--ui-stroke-primary", "--ui-accent", "--ui-yellow"}

HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
FALLBACK = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*,\s*(#[0-9a-fA-F]{3,8})\s*\)")
ANY_VAR = re.compile(r"var\(\s*(--[a-z0-9-]+)")

if not SRC.exists():
    print(f"the panel's source is not here: {SRC}")
    sys.exit(1)
lines = SRC.read_text(encoding="utf-8", errors="replace").splitlines()

# ---- 1. no bare surface colours --------------------------------------------------------------
# A literal is "bare" when it is not inside a var() fallback and not a declared status colour.
bare: list[str] = []
for n, line in enumerate(lines, 1):
    if line.strip().startswith(("//", "*", "/*")):
        continue
    fallbacks = {m.group(2).lower() for m in FALLBACK.finditer(line)}
    for m in HEX.finditer(line):
        hexv = m.group(0).lower()
        if hexv in fallbacks or hexv in STATUS:
            continue
        bare.append(f"{n}: {hexv}  {line.strip()[:96]}")

check("no colour in the panel is a literal that forgot which theme it was written for",
      not bare, "\n         ".join(bare[:6]) or "")
check("... and the file was actually read, so an empty answer means something",
      len(lines) > 1000, f"{len(lines)} lines")

# ---- 2. every var() names one the app injects, or carries a fallback -------------------------
# A name is fine when the app injects it, and it is fine when the author wrote what to use
# instead — `var(--font-mono, ui-monospace, monospace)` is a choice. What is not fine is a name
# with nothing behind it: it resolves to nothing, `color` inherits by accident, `background`
# vanishes, and the bug is invisible in whichever theme the author happened to be using.
unknown: list[str] = []
for n, line in enumerate(lines, 1):
    for m in re.finditer(r"var\(\s*(--[a-z0-9-]+)(\s*,)?", line):
        name, has_fallback = m.group(1), bool(m.group(2))
        if name in KNOWN_VARS or name in PANEL_VARS or has_fallback:
            continue
        # A name written as another variable's fallback -- `var(--foreground, var(--ui-…))` -- is
        # doing its job: it is the answer for a host that injects nothing. The problem is a name
        # that *leads*, which has nothing behind it at all.
        if re.search(r"var\(\s*--[a-z0-9-]+\s*,\s*$", line[:m.start()]):
            continue
        unknown.append(f"{n}: {name}")
seen_unknown = sorted({u.split(": ")[1] for u in unknown})
check("every theme variable the panel uses is one the app defines, or carries a fallback",
      not unknown,
      f"{len(unknown)} use(s) of {len(seen_unknown)} name(s) with nothing behind them: "
      + ", ".join(seen_unknown[:8]))

# ---- 2c. a name the panel allows itself is actually defined -----------------------------------
# The allowance above is only honest while each name in it is really declared in this
# stylesheet. A name left in PANEL_VARS after the rule that declared it was deleted is the
# same bug as a name with nothing behind it, and it would pass check 2 forever.
_join = "\n".join(lines)
_undeclared = sorted(v for v in PANEL_VARS if not re.search(re.escape(v) + r"\s*:", _join))
check("every variable the panel defines for itself is actually declared in the panel",
      not _undeclared, ", ".join(_undeclared))

# ---- 2b. a retired name never leads a chain ---------------------------------------------------
# `var(--foreground, var(--ui-text-primary))` is a decision: the app's value, with the old one
# behind it. `var(--ui-text-primary)` alone is the bug that made a progress bar invisible.
leading: list[str] = []
for n, line in enumerate(lines, 1):
    for m in re.finditer(r"var\(\s*(--ui-[a-z0-9-]+)", line):
        before = line[:m.start()]
        if not re.search(r"var\(\s*--[a-z0-9-]+\s*,\s*$", before):
            leading.append(f"{n}: {m.group(1)}  {line.strip()[:88]}")
check("no retired variable name is used without a live one in front of it",
      not leading, "\n         ".join(leading[:6]) or "")

# ---- 3. the status allow list is honest -------------------------------------------------------
# Every status colour on the list has to be *used*, or the list is a museum: a colour kept "just
# in case" is a colour nobody has decided about, which is the thing this file is against.
text = "\n".join(lines)
unused = [f"{h} ({why})" for h, why in STATUS.items() if h not in text.lower()]
check("no status colour is on the list without being used in the panel",
      not unused, ", ".join(unused))

# ---- 4. the surfaces that were wrong stay right ----------------------------------------------
# Named individually because these are the three an audit found and a fix is only a fix if it is
# still there: the viewer's letterbox, the "no screen yet" placeholder, and the video backdrop.
pairs = [
    (".ah-screenimg", "--card", "the screen viewer's frame"),
    (".ah-screenblank", "--card", "the 'no screen to show' placeholder"),
    (".ah-vframe", "--card", "the video preview's backdrop"),
]
for sel, var, what in pairs:
    # Every rule mentioning the selector, not just the first: a class also appears in descendant
    # selectors (`.ah-mbody .ah-screenimg { ... }`) which would otherwise answer for the rule that
    # actually paints the surface.
    # Built by concatenation: an f-string cannot carry a regex with braces without the braces
    # being eaten by the format syntax, and the first version of this check silently matched
    # nothing and read as "the surface is fine".
    rules = re.findall(re.escape(sel) + r"\s*\{[^}]*\}", text)
    body = rules[-1] if rules else ""
    check(f"{what} is drawn on the app's surface, not on a colour chosen for one theme",
          var in body, body.replace("\n", " ")[:110])

# ---- 5. nothing hardcodes an app-level surface in the donor halves either ---------------------
# The folded-in halves are spliced into the same stylesheet, so a literal there is the same bug.
halves = sorted(PLUGIN.glob("desktop/*.pre-splice*")) + sorted(PLUGIN.glob("desktop/*.half"))
half_bare: list[str] = []
for h in halves:
    for n, line in enumerate(h.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        fallbacks = {m.group(2).lower() for m in FALLBACK.finditer(line)}
        for m in HEX.finditer(line):
            if m.group(0).lower() in fallbacks or m.group(0).lower() in STATUS:
                continue
            half_bare.append(f"{h.name}:{n}: {m.group(0)}")
check("the donor halves hold to the same rule (they are spliced into the same stylesheet)",
      not half_bare, "\n         ".join(half_bare[:6]) or "")

print(f"\n{PASS} ok, {FAIL} failed")
sys.exit(1 if FAIL else 0)
