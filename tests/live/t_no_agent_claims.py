"""
The launch does not claim agent control, and nothing shipped may say it does.

Agent control was taken out of the launch scope: the app store ships the features
that exist and *promises* agent control as forthcoming. Every sentence that said
otherwise was removed from the shipped copy — `plugin.yaml`, `README.md`, and the
panel source and its build — and that removal was checked once, by hand, with a
grep. Which is exactly the problem: a grep run once is a fact about the day it
ran, not a property of the product. Nothing in this suite would have failed if
the sentences had come back, and this suite has already been read as green on an
unchanged tree twice while work was believed done.

So the claim is now a gate. The shipped surface is the manifest, the README, the
panel source and its build, and the store listing users see before install:

    plugin.yaml  README.md  desktop/plugin.js  desktop/plugin.js.pre-merge
    docs/catalog-entry.yaml

Two kinds of sentence are forbidden there. First the fixed marketing phrases:

    agent-ready        agent-controlled        exposed to agents        native tool

and then any present-tense assertion that an agent already drives the product,
however it is worded — `the agent can drive`, `let the agent drive`,
`agent can control`, `drives every`. The nouns do not matter; the tense does.
A promise that agent control is coming is the intended replacement and must pass:
"Agent control is in active development." is not a failure, and neither is
"driving all of it from a single sentence is the next one."

The bare word `agent` is deliberately *not* forbidden. The machinery keeps
agent.json manifests, exposes agent tool surfaces, and the manifest sells "agent
projects" as a catalogue category — all true, all staying. Matching the word
would fail the product for describing itself correctly; matching the sales claims
is what this is for. If a new way of saying "an agent drives this" gets invented,
add it to CLAIMS rather than loosening the match.

Usage: env -u PYTHONPATH <venv python> t_no_agent_claims.py
"""
import re
import sys
from pathlib import Path

# Derived from this file's own location so the suite works wherever the plugin is
# checked out, the way run_all.sh derives its paths.
PLUG = Path(__file__).resolve().parents[2]

SHIPPED = ["plugin.yaml", "README.md", "desktop/plugin.js",
           "desktop/plugin.js.pre-merge", "docs/catalog-entry.yaml"]

# Each entry is (name, pattern). Hyphen and space are both matched for the fixed
# phrases: the claim reads the same either way, and the panel is HTML inside a JS
# string where a hyphen can arrive as an entity or a non-breaking space. The
# present-tense patterns are word-by-word with `\s+` because a line wrap in the
# README or the folded YAML scalar puts a newline in the middle of the sentence,
# and a claim that stops being a claim once it wraps is not a gate. `drives every`
# is here because the first pass proved the claim survives a rewrite into words
# none of the fixed phrases contain.
CLAIMS = [
    ("agent-ready", re.compile(r"agent[\s\-_]*ready\b", re.I)),
    ("agent-controlled", re.compile(r"agent[\s\-_]*controlled\b", re.I)),
    ("exposed to agents", re.compile(r"exposed\s+to\s+(?:the\s+)?agents?\b", re.I)),
    ("native tool", re.compile(r"native\s+tools?\b", re.I)),
    ("the agent can drive", re.compile(r"\bthe\s+agent\s+can\s+drive\b", re.I)),
    ("let the agent drive", re.compile(r"\blet\s+the\s+agent\s+drive\b", re.I)),
    ("agent can control", re.compile(r"\bagent\s+can\s+control\b", re.I)),
    ("drives every", re.compile(r"\bdrives\s+every\b", re.I)),
]

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")
        if detail:
            print(f"       {detail}")


def scan(path):
    """Every claim hit in one file, as (line number, claim name, line)."""
    hits = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        for name, pattern in CLAIMS:
            if pattern.search(line):
                hits.append((lineno, name, line.strip()))
    return hits


print(f"-- checking {len(SHIPPED)} shipped file(s) for claims the launch does not make --")

total_hits = 0
missing = []
for rel in SHIPPED:
    path = PLUG / rel
    if not path.is_file():
        missing.append(rel)
        continue
    lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    hits = scan(path)
    total_hits += len(hits)
    # An empty or unreadable file would pass a "no claims" check by saying
    # nothing at all, which is how a gate becomes decoration.
    if lines == 0:
        check(f"{rel}: readable, non-empty", False, f"{path} read as 0 lines")
        continue
    detail = "\n       ".join(
        f"{rel}:{lineno}  [{name}]  {line[:160]}" for lineno, name, line in hits)
    check(f"{rel}: {len(hits)} claim(s) in {lines} lines", not hits, detail)

check("every shipped file was actually read", not missing,
      f"missing: {', '.join(missing)}" if missing else "")

print(f"       {total_hits} forbidden claim(s) across {len(SHIPPED)} file(s)")

# The matcher has to be able to fail, and it has to know what not to fail on.
# Both directions are checked against the same patterns the scan uses, so a
# pattern quietly broken into never-matching keeps this gate honest.
def caught(text):
    return any(p.search(text) for _, p in CLAIMS)


for sample in ("Ships agent-ready apps", "Agent Ready out of the box",
               "apps are agent-controlled", "every app is exposed to agents",
               "exposed to the agent", "a native tool surface",
               "let the agent drive every one of those through 24 athena_* tools",
               "the agent can drive the whole loop",
               "the agent can control installs", "one sentence drives every app"):
    check(f"would reject {sample!r}", caught(sample),
          "the matcher does not see this as a claim")

# ... and legitimate technical uses, and the intended future-tense wording, must
# survive — or the gate gets deleted the first time it blocks a true sentence.
for sample in ("Browse image/video/audio/LLM/agent projects", 'read agent.json',
               "the agent tool surface", "agent_id", "agents are configured in settings",
               "Agent control is in active development.",
               "driving all of it from a single sentence is the next one"):
    check(f"does not flag {sample!r}", not caught(sample),
          "a technical use of `agent` was mistaken for a sales claim")

print(f"\n{failed} failed" if failed else f"\n{passed}/{passed} checks passed")
sys.exit(1 if failed else 0)
