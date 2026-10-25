"""
The claims in the docs, checked against what is actually in the tree.

A number written in prose is a claim, and these are the ones nothing used to
check. Every one of them was wrong at least once:

 * README quoted `t_agent_tools` at 154 checks long after it ran 161, and
   `t_screen.py` at 60 while it ran 62 — a file named the documented count and a
   different line printed the real one, and no step ever read both;
 * `NOTICE` said 502 packages and named two packages as having no licence, while
   `THIRD-PARTY.md`, its own sibling, said 501 and named one. The tree says 501
   and one — counting every distinct `name@version` under `engine/node_modules`,
   nested copies included, which is the rule `THIRD-PARTY.md` states. The second
   "package", `beep-boop@1.2.3`, is a fixture inside
   `github-from-package/example/package.json`, which is a file shipped inside
   another package rather than an installed dependency;
 * README and the catalog entry both advertised 23 tools against 24 registered,
   and the README's tool table was missing a row.

`run_all.sh` writes each test's own summary line to a transcript and passes it
here as the first argument, so the per-file counts are compared against what the
tests actually printed in that run instead of against a number typed once. Run it
by hand and it checks everything except those counts, saying so rather than
quietly passing.

Usage: env -u PYTHONPATH <venv python> check_doc_counts.py [transcript]
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")
PLUG = Path(str(HERMES_ROOT) + r"/plugins/athena")

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


README = (PLUG / "README.md").read_text(encoding="utf-8")
NOTICE = (PLUG / "NOTICE").read_text(encoding="utf-8")
THIRD = (PLUG / "THIRD-PARTY.md").read_text(encoding="utf-8")
CATALOG = (PLUG / "docs" / "catalog-entry.yaml").read_text(encoding="utf-8")
MANIFEST = (PLUG / "plugin.yaml").read_text(encoding="utf-8")
INIT = (PLUG / "__init__.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The vendored engine's package and licence counts, re-derived from the tree.
# ---------------------------------------------------------------------------
print("== the vendored engine, counted again ==")


def licence_of(doc: dict):
    """The licence a package declares, in either of npm's two spellings."""
    lic = doc.get("license") or doc.get("licenses")
    if isinstance(lic, list):
        lic = lic[0] if lic else None
    if isinstance(lic, dict):
        lic = lic.get("type")
    return lic


def inventory() -> tuple[dict, list]:
    """Every distinct name@version in the tree, plus the ones with no licence.

    The rule is the one `THIRD-PARTY.md` states: every `package.json` under
    `engine/node_modules`, nested copies included, so a package that also appears
    inside another package's own `node_modules` is one entry with two files. Two
    kinds of file are not packages and are left out: the entry-point manifests
    that carry subpaths and no version (`rxjs/ajax` and friends), and the fixture
    inside `github-from-package/example`.
    """
    root = PLUG / "engine" / "node_modules"
    seen: dict[str, object] = {}
    for p in root.rglob("package.json"):
        if "example" in p.parts:
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        name, version = doc.get("name"), doc.get("version")
        if not name or not version:
            continue
        seen.setdefault(f"{name}@{version}", licence_of(doc))
    return seen, [k for k, v in seen.items() if not v]


pkgs, unlicensed = inventory()
N = len(pkgs)
print(f"       {N} packages, {len(unlicensed)} of them with no licence field: "
      f"{', '.join(sorted(unlicensed)) or 'none'}")

check(f"THIRD-PARTY.md says {N} packages", f"{N} packages" in THIRD)
check(f"NOTICE says {N} packages", f"{N} packages" in NOTICE)
# The README used to carry this count too. It is a plugin description now and
# quotes no package count — the number lives in THIRD-PARTY.md and NOTICE, both
# checked above, and `inventory()` is what re-derives it from the tree. A README
# that claimed a *different* number would be the only way to get this wrong, so
# if a count appears there it still has to agree.
_rm_counts = set(int(m) for m in re.findall(r"(\d+)\s+(?:transitive\s+)?packages", README))
check("any package count the README does carry agrees, or it carries none",
      not _rm_counts or _rm_counts == {N},
      f"README says {sorted(_rm_counts)}, the tree holds {N}")

bullets = re.findall(r"^- `([^`]+)` — ", THIRD, re.M)
check(f"THIRD-PARTY.md lists exactly the {N} packages that are here",
      len(bullets) == N and set(bullets) == set(pkgs),
      f"listed {len(bullets)}; not in the tree: {sorted(set(bullets) - set(pkgs))[:5]}; "
      f"in the tree but not listed: {sorted(set(pkgs) - set(bullets))[:5]}")

check("every unlicensed package is named, and only those",
      all(u in THIRD for u in unlicensed) and len(unlicensed) == 1
      and unlicensed == ["copy-paste@2.2.0"], str(unlicensed))
# The fixture is inside another package's example directory. It is not installed,
# it has no version of its own in npm's sense, and naming it as a dependency was
# a mistake worth keeping a gate against.
# The fixture lives in another package's `example/` directory. Counting it as a
# dependency was the mistake; naming it *while saying why it is not one* is the
# fix, so the check is that it never appears without that explanation.
check("the example fixture is not reported as a package",
      "beep-boop" not in NOTICE or "is not a package" in NOTICE)


# ---------------------------------------------------------------------------
# 2. The tool surface the manifest declares, and the one hook both manifests
#    have to name.
# ---------------------------------------------------------------------------
print("\n== the agent tools ==")
# The registered tool name is the first argument of each `_schema(...)`, not the
# dict key: two keys are internal handles whose names differ (`app_verify`
# declares `athena_capabilities`, `history` declares `athena_runs`), and
# comparing the raw keys against a list produced a phantom difference in both
# directions.
registered = re.findall(r'^\s{4}"\w+": _schema\(\s*\n\s*"(athena_\w+)"', INIT, re.M)
manifest_block = MANIFEST.split("provides_tools:", 1)[1] if "provides_tools:" in MANIFEST else ""
declared = re.findall(r"^\s+- (\w+)$", manifest_block.split("\n\n", 1)[0], re.M)

print(f"       registered {len(registered)}, manifest {len(declared)}")
check("the manifest declares every tool that is registered",
      set(declared) == set(registered),
      f"only declared: {sorted(set(declared) - set(registered))}; "
      f"missing: {sorted(set(registered) - set(declared))}")


def _flow_list(text: str, key: str) -> "list[str] | None":
    """The values of a `key: [a, b]` flow list, or None when the key is absent."""
    m = re.search(rf"^\s*{key}:\s*\[([^\]]*)\]", text, re.M)
    if not m:
        return None
    return [s.strip() for s in m.group(1).split(",") if s.strip()]


# The hook surface, declared where a reader looks for it. `__init__.py` registers
# `pre_tool_call` (the approval gate in front of every Athena tool); `hermes
# plugins doctor` warns when the manifest does not say so, and the catalog schema
# carries the same field. A manifest that under-reports its own surface is the
# noise that hides a real warning the day there is one.
registered_hooks = re.findall(r'register_hook\(\s*"(\w+)"', INIT)
check(f"plugin.yaml declares the hook(s) the plugin registers: {', '.join(registered_hooks) or 'none'}",
      bool(registered_hooks) and _flow_list(MANIFEST, "provides_hooks") == registered_hooks,
      f"manifest says {_flow_list(MANIFEST, 'provides_hooks')}, registered {registered_hooks}")
check("…and so does the catalog entry, so the doctor has nothing to warn about",
      bool(registered_hooks) and _flow_list(CATALOG, "provides_hooks") == registered_hooks,
      f"catalog says {_flow_list(CATALOG, 'provides_hooks')}, registered {registered_hooks}")


# ---------------------------------------------------------------------------
# 3. What the README has to disclose.
#
# The README is a plugin description now, not a feature sheet: the package count
# lives in THIRD-PARTY.md and NOTICE (checked above) and it carries no tool table
# or tool count. What it *must* still say — because a plugin that runs a
# stranger's install script on this machine, installs an engine from npm, reaches
# named endpoints, runs a real OS and pushes to GitHub is a plugin whose reader
# has to be told so — is the list of things it does. Each of these was added
# under the store review, and each is a claim that would become a lie by omission
# if it were quietly dropped, so each is checked against the README's own words.
# ---------------------------------------------------------------------------
print("\n== what the README has to disclose ==")
# Emphasis and code marks are stripped first: these are prose claims, and a `**`
# moving around inside a sentence must not read as the claim having gone.
plain = re.sub(r"[*`]", "", re.sub(r"\s+", " ", README)).lower()
disclosures: list[tuple[str, list[str]]] = [
    ("third-party install scripts run as the user, with their privileges",
     ["third-party code runs as you", "that repository's own install script",
      "your privileges"]),
    ("the engine comes from npm at a pinned version and is not vendored",
     ["not vendored here", "pinned to one exact version",
      "installs that exact version from npm"]),
    ("which network endpoints it contacts, by name",
     ["the endpoints it contacts", "api.github.com", "registry.npmjs.org",
      "cloud-images.ubuntu.com"]),
    ("its WSL / QEMU machines really execute commands on this computer",
     ["wsl distribution or a qemu guest", "execute commands and send keystrokes"]),
    ("GitHub publishing, and that it asks before it pushes",
     ["athena_publish", "pushes to a github repository you name",
      "asks before it pushes"]),
]
for _label, _needles in disclosures:
    _missing = [n for n in _needles if n not in plain]
    check(f"the README says {_label}", not _missing, f"missing: {_missing}")


# ---------------------------------------------------------------------------
# 4. The per-file check counts the README quotes, against this run's output.
# ---------------------------------------------------------------------------
print("\n== what the README says each test checks ==")
flat = re.sub(r"\s+", " ", README)
claims: dict[str, set[int]] = {}
for m in re.finditer(r"`(t_\w+)\.py`[^`]{0,160}?(\d+)\s+checks", flat):
    claims.setdefault(m.group(1), set()).add(int(m.group(2)))

counts: dict[str, int] = {}
arg = sys.argv[1] if len(sys.argv) > 1 else ""
if arg and not Path(arg).is_file():
    # Nobody passes a transcript path for fun. If the file is not readable the comparison
    # cannot happen, and a checker that announces "not compared" and exits 0 is the reason
    # the counts it exists to verify went stale in the first place.
    check("the counts transcript can be read", False,
          f"asked to compare against {arg!r}, which is not a readable file")
elif arg and Path(arg).is_file():
    # A summary line reads `<test>\t<test>: 9 passed, 0 failed`, so the first digits in the
    # tail are frequently inside the test's *own name* — `t_i18n: 9 passed` searched naively
    # yields 18, and a test whose name contains a number would have been reported as having
    # run that many checks. The name is dropped before the count is read. Verified by the
    # self-check below, because a reader that silently reads the wrong number is the same
    # failure this file exists to catch, one level down.
    def counted(tail: str) -> int | None:
        rest = re.sub(r"^[A-Za-z_][\w./-]*\s*[:/]?\s*", "", tail)
        got = re.search(r"\d+", rest)
        return int(got.group(0)) if got else None

    check("the transcript reader takes the count, not a digit out of the test's name",
          counted("t_i18n: 9 passed, 0 failed") == 9
          and counted("t_pipeline: 33 passed, 0 failed") == 33
          and counted("217/217 checks passed (4 skipped)") == 217
          and counted("41 check(s) passed") == 41
          and counted("t_idle\t0 check(s) failed".split("\t")[0]) is None,
          f"read {counted('t_i18n: 9 passed, 0 failed')} from 't_i18n: 9 passed, 0 failed'")

    for line in Path(arg).read_text(encoding="utf-8", errors="replace").splitlines():
        label, _, tail = line.partition("\t")
        got = counted(tail)
        if label and got is not None:
            counts[label] = got
    compared = 0
    for test, said in sorted(claims.items()):
        if test not in counts:
            continue
        compared += 1
        check(f"{test}: README says {'/'.join(str(s) for s in sorted(said))}, it ran {counts[test]}",
              len(said) == 1 and counts[test] in said,
              f"README says {sorted(said)}, the run printed {counts[test]}")
    if claims and not compared:
        check("the transcript matched the README's claims", False,
              "no claim in the README matched a test that just ran")
    # The transcript itself still has to be worth reading: a run that produced no
    # summary lines at all means the plumbing broke, whatever the README says.
    if not counts:
        check("the transcript carries run summaries to read", False,
              f"{arg} held no '<test>\\t<summary>' lines")
    # The README is a plugin description now and quotes no per-file counts, so
    # there may be nothing here to compare. That is not the same as the check
    # having run and passed, so it is said out loud rather than counted as a
    # match — and it is not the README's hold on this file any more: the
    # disclosures above are, and they still fail if one is dropped. Should the
    # README quote counts again, every one of them is compared exactly as before.
    if claims:
        print(f"       {compared} of {len(claims)} documented count(s) compared against this run")
        print(f"       (checked for: {', '.join(sorted(claims))})")
    else:
        print(f"       the README quotes no per-file counts now (it is a plugin "
              f"description); nothing to compare against this run's "
              f"{len(counts)} summary line(s)")
else:
    print("  skip per-file counts        no transcript given — "
          + (f"{len(claims)} documented count(s) not compared (run_all.sh does compare them)"
             if claims else "and the README quotes none to compare")
          )
    print(f"       (would check: {', '.join(sorted(claims)) or 'nothing'})")

print(f"\ncheck_doc_counts: {PASS} claim(s) checked, {FAIL} failed")
if FAILURES:
    for f in FAILURES:
        print(f"   - {f}")
sys.exit(1 if FAIL else 0)
