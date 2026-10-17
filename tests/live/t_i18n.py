"""
Every word the folded-in tabs ask for by name.

The Files and System tabs are not Athena's own code: they are two whole plugins'
UIs folded in by `merge_ui.py`, and each carries its own dictionary. Nothing
checks those dictionaries against the code that reads them, and a missing key is
the quietest failure in the panel — `t('emptyBin')` with no `emptyBin` in the
dictionary draws the word *emptyBin* on a button, which looks like a styling
problem, not like a bug, and no route returns an error for it.

Two more ways the fold can go wrong, both silent:

* The registration spreads one dictionary over the other —
  `{ ...AthenaFiles.EN, ...AthenaSystem.EN }` — so two tabs naming the same key
  differently means one of them quietly says the other's words, in every place
  it is used. `up` in both dictionaries agreeing is fine; disagreeing is not.
* A key only one side ever had goes on working while the other side's tab shows
  the key itself.

So this file reads the built panel, finds each block's dictionary and the keys
that block asks for, and holds the two against each other. It reads
`desktop/plugin.js`, the file that ships — not the sources it is assembled from —
because the splice is what produces the failure.

Run with `env -u PYTHONPATH -u ATHENA_HOME -u ATHENA_PORT` from this directory.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")
PANE = Path(str(HERMES_ROOT) + r"/plugins/athena/desktop/plugin.js")

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


def block(src: str, header: str) -> str:
    """The body of an IIFE, from its first line to the line that closes it."""
    start = src.index(header)
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i]
    raise AssertionError(f"{header}: never closed")


def balanced(src: str, open_at: int) -> str:
    """The text between a `{` or `(` and the one that closes it.

    Quotes are respected: a dictionary value is `'Recycle Bin'`, and one stray
    brace inside a sentence would end the block early and make every check below
    read half a file while still passing.
    """
    pairs = {"{": "}", "(": ")", "[": "]"}
    want = pairs[src[open_at]]
    depth, i, quote = 0, open_at, None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in pairs:
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                if ch != want:
                    raise AssertionError(f"unbalanced at {open_at}")
                return src[open_at + 1:i]
        i += 1
    raise AssertionError(f"never closed at {open_at}")


KEY = re.compile(r"(?:^|[\s{,}])([A-Za-z_][A-Za-z0-9_]*)\s*:")


def dictionary(body: str, name: str = "EN") -> dict:
    """{key: value-source} for a flat dictionary in the block."""
    at = body.index(f"const {name} = {{")
    text = balanced(body, body.index("{", at))
    out = {}
    # A line holds several keys (`newFolder: 'New folder', newFile: 'New file',`)
    # and values may be strings, arrows or numbers, so entries are split on the
    # key pattern and each value runs to the next key.
    marks = [(m.start(1), m.end(1)) for m in KEY.finditer(text)]
    for n, (a, b) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(text)
        out[text[a:b]] = text[b:end].lstrip().lstrip(":").strip().rstrip(",").strip()
    return out


def asks_for(body: str) -> tuple:
    """(keys asked for by name, how many asks were built some other way)."""
    keys, dynamic = set(), 0
    for m in re.finditer(r"\bt\(\s*", body):
        args = balanced(body, m.end() - 1)
        lit = re.match(r"\s*['\"]([^'\"]+)['\"]\s*$", args)
        if lit:
            keys.add(lit.group(1))
        else:
            dynamic += 1
    return keys, dynamic


src = PANE.read_text(encoding="utf-8")
print(f"reading {PANE.name}: {len(src)} chars")

files = block(src, "const AthenaFiles = (() => {")
system = block(src, "const AthenaSystem = (() => {")

# ------------------------------------------------------------ the splice itself
check("both folded-in tabs are in the built panel",
      len(files) > 10_000 and len(system) > 1_000,
      f"files {len(files)} chars, system {len(system)} chars")
check("both tabs are registered with the panel's own i18n",
      "ctx.i18n.register({ en: { ...AthenaFiles.EN, ...AthenaSystem.EN } })" in src.replace("\n", " ").replace("  ", " ")
      or ("...AthenaFiles.EN" in src and "...AthenaSystem.EN" in src),
      "the spread that silently decides between two same-named keys is not there")

# ------------------------------------------------- word asked for, word present
def words(block_text: str, label: str) -> dict:
    en = dictionary(block_text)
    asked, dynamic = asks_for(block_text)
    missing = sorted(k for k in asked if k not in en)
    empty = sorted(k for k, v in en.items() if v in ("''", '""', "``", ""))
    unused = sorted(k for k in en if k not in asked)
    print(f"  note {label}: {len(en)} words declared, {len(asked)} asked for by name, "
          f"{dynamic} built at the call site")
    check(f"{label}: every word it asks for by name is in its dictionary ({len(asked)})",
          not missing, ", ".join(missing))
    check(f"{label}: none of its words is blank", not empty, ", ".join(empty))
    # Unused words are not a failure — a dictionary may be written before its tab
    # uses it — but a handful is a dictionary and a pile is a second spelling of
    # words that are already there, which is how a tab ends up saying 'Recycle
    # Bin' in one place and 'Bin' in another.
    check(f"{label}: its dictionary is not mostly unused ({len(unused)} of {len(en)})",
          len(unused) <= max(4, len(en) // 4), f"{len(unused)} unused: {', '.join(unused[:10])}")
    return en


file_en = words(files, "the Files tab")
sys_en = words(system, "the System tab")

# ------------------------------------------------ two dictionaries, one namespace
# `{ ...A, ...B }` keeps B's value for any key in both. Where the two disagree the
# later one wins everywhere the name is used, so the Files tab would say the
# System tab's words and neither dictionary would look wrong.
shared = sorted(set(file_en) & set(sys_en))
clash = [f"{k}: files {file_en[k]!r} vs system {sys_en[k]!r}"
         for k in shared if file_en[k] != sys_en[k]]
check(f"where the two tabs share a word they agree on it ({len(shared)} shared)",
      not clash, "; ".join(clash[:5]))
print(f"  note shared words: {', '.join(shared) if shared else 'none'}")

print(f"\nt_i18n: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("failed: " + ", ".join(FAILURES))
sys.exit(1 if FAIL else 0)
