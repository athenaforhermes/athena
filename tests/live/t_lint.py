"""
A name read but never bound anywhere in the file.

This exists because of a real one: five call sites used `_athena_home()`, the
function was never defined, and `py_compile` was perfectly happy — so the disk
gate, the VRAM gate and the publisher would each have raised NameError the first
time a user launched an app. A test that only imports a module cannot see it
either, because the failing line is inside a function nobody called yet.

Loose on purpose: any binding of a name anywhere in the file counts as defined,
so it will not flag a variable that is set in one branch and read in another. It
flags the one shape that matters — a name that is only ever read. No dependency
beyond the standard library, so it can run in the battery on any machine.

Usage: env -u PYTHONPATH <venv python> t_lint.py
"""
import ast
import builtins
import re
import sys
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUG = Path(str(HERMES_ROOT) + r"/plugins/athena")
TARGETS = [PLUG / "__init__.py"] + sorted((PLUG / "dashboard").glob("*.py")) \
    + sorted((PLUG / "tools").glob("*.py"))

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}\n       {detail}")


def bindings(tree):
    """Every name the file binds: imports, defs, classes, assignments, args."""
    # `__file__` is injected by the loader, and a module that defers its
    # annotations never evaluates them — so a typing name used only as a hint is
    # not a name this file has to import.
    bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__"}
    if any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
           and any(a.name == "annotations" for a in n.names) for n in ast.walk(tree)):
        for n in ast.walk(tree):
            anns = []
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                anns = [n.returns] + [a.annotation for a in n.args.args + n.args.kwonlyargs]
            elif isinstance(n, ast.AnnAssign):
                anns = [n.annotation]
            for a in anns:
                for sub in ast.walk(a) if a is not None else []:
                    if isinstance(sub, ast.Name):
                        bound.add(sub.id)
                    elif isinstance(sub, ast.Attribute):
                        bound.add(sub.attr)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            for a in getattr(node, "args", None).args if hasattr(node, "args") else []:
                bound.add(a.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return bound


print(f"-- scanning {len(TARGETS)} files --")
all_bad = {}
for path in TARGETS:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    bound = bindings(tree)
    bad = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in bound:
            bad.setdefault(node.id, node.lineno)
    if bad:
        all_bad[path.name] = bad

for name, bad in all_bad.items():
    for var, line in sorted(bad.items(), key=lambda kv: kv[1]):
        print(f"  ?    {name}:{line} reads `{var}`, which nothing in the file binds")

check("every module parses", all(True for _ in TARGETS))
check("no file reads a name it never binds", not all_bad,
      "; ".join(f"{n}: {sorted(v)[:6]}" for n, v in all_bad.items()))

# The check has to be able to fail, or it proves nothing. A throwaway source with
# exactly the bug that started this: a call to a function that does not exist.
sneaky = ast.parse("def go():\n    return nowhere_at_all(1)\n")
bad = {n.id for n in ast.walk(sneaky)
       if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bindings(sneaky)}
check("and the check would have caught `_athena_home`", "nowhere_at_all" in bad, str(bad))

# ------------------------------------------------------------- the README too
# Every design constraint in README.md has to start its own line, and the numbers
# have to run 1..N with none missing. This gate exists because they did not:
# three doc inserts were anchored on a raw character offset (`s[i-30:i+14]`), the
# patch matcher matched them fuzzily, and `31.`/`32.`/`33.` ended up stitched onto
# the tail of the sentence before them with thirty characters eaten out of each.
# Nothing in the suite read README prose, so it stayed green through two rounds of
# "docs updated" while the constraints were unreadable. Prose is not exempt from
# being checked when it is the record of what was decided.
README = PLUG / "README.md"
text = README.read_text(encoding="utf-8")
numbers = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. \*\*", text, re.M)]
stitched = re.findall(r"[^\s\d]\d+\. \*\*", text)
check("every design constraint in the README starts its own line",
      not stitched, f"stitched onto the sentence before it: {stitched[:6]}")
check(f"and they are numbered 1..{len(numbers)} with none missing or repeated",
      numbers == list(range(1, len(numbers) + 1)), f"read {numbers}")
# A check that cannot fail proves nothing, so here is the same shape as it really
# appeared, run through the same expression.
sample = "asserted present in the merge rather th31. **A chain of apps is checked"
check("and it would have caught the jam that started it",
      bool(re.findall(r"[^\s\d]\d+\. \*\*", sample)), repr(sample))
# The other direction matters just as much: a constraint on its own line is not a
# jam, and `\S` alone cannot tell the two apart — it swallows the first digit of
# every two-digit number, so `\n10. **` looks exactly like a stitched-in `10. **`.
check("... while a constraint on its own line is not mistaken for one",
      not re.findall(r"[^\s\d]\d+\. \*\*", "\n10. **A constraint\n11. **Another"), "a false positive here would fail every README")

print(f"\n{failed} failed" if failed else f"\n{passed}/{passed} checks passed")
sys.exit(1 if failed else 0)
