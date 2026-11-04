#!/usr/bin/env python3
"""The environment a child process is handed -- and the rule that keeps it that way.

An installed Athena app is arbitrary code running as this person; that is the design, and
`docs/threat-model.md` says so. What is checked here is the part Athena keeps for itself.
Every provider key Hermes loaded sits in *Athena's* own process environment, and
`subprocess.run` without `env=` hands a child that entire environment by default -- so a
repository cloned from a stranger can hold a live API key that nobody typed into it.

Three sites did exactly that, found in review after the file had already been through a
security pass, and each for its own reason:

  * `_app_menu` runs `menu.js`, which `require()`s the app's own launcher -- the app's code
    executes *inside* the child that was inheriting everything.
  * `_venv_smoke` runs the app's virtualenv `python`, whose `.pth` files execute at
    interpreter startup -- the app's code again, before `-c` runs a line.
  * `_pull_run` built its environment by *subtracting* two names from `os.environ`. That is
    a denylist: it holds for the names somebody thought of, and a key added tomorrow leaks
    in silence. The fix is an allowlist, which fails closed instead.

The *class* is checked too, not only the three: every spawn in the plugin's Python that
passes no `env=` is counted per function and matched against the table below, and a new one
fails this file whichever function it lands in -- including one spelled through an alias,
which is why the detector reads each file's own imports instead of looking for the word
`subprocess`, and why the spellings it must catch are planted and counted first. That is the
mechanism whose absence let a fourth site sit through the review that named the first three:
`naming.tracked_by_git`, which runs `git` inside somebody else's checkout -- and git reads
that checkout's own `.git/config`, where `core.fsmonitor` names a program to run, before it
answers.

Usage: env -u PYTHONPATH -u ATHENA_HOME <venv python> t_child_env.py
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")
PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")
sys.path.insert(0, str(PLUGIN / "dashboard"))

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
        print(f"  FAIL {name}" + (f"  [{detail}]" if detail else ""))


def _src(rel: str) -> str:
    return (PLUGIN / rel).read_text(encoding="utf-8", errors="replace")


# Everything that starts a process, under both the names it goes by. `os.system` is on the
# list because it is the same act with less ceremony; the plugin has none today, and a
# detector that only knows the shape somebody happened to use reports the tree clean while
# looking at part of it.
_SPAWN_ATTR = {
    "subprocess": ("run", "Popen", "call", "check_call", "check_output"),
    "os": ("system", "popen", "startfile", "spawnv", "spawnve", "spawnl", "spawnle",
           "execv", "execve", "execl", "execlp", "execvp"),
}


def _roots(tree: ast.AST) -> tuple:
    """Which local names mean `subprocess` or `os`, and which names came in from them.

    An alias is how this rule gets around: `import subprocess as sp` with `sp.run(...)` is
    the same spawn as `subprocess.run(...)`. So the file's own imports are read.
    """
    roots: dict = {}
    named: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in _SPAWN_ATTR:
                    roots[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module in _SPAWN_ATTR:
            for a in node.names:
                if a.name in _SPAWN_ATTR[node.module]:
                    named[a.asname or a.name] = a.name
    return roots, named


def _spawns(node: ast.AST, roots: dict, named: dict) -> list:
    """Every process this node starts, as (call, passes_env)."""
    found = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            module = roots.get(f.value.id)
            if module is None or f.attr not in _SPAWN_ATTR[module]:
                continue
        elif isinstance(f, ast.Name) and f.id in named:
            if named[f.id] not in _SPAWN_ATTR["subprocess"] + _SPAWN_ATTR["os"]:
                continue
        else:
            continue
        found.append((sub, "env" in {k.arg for k in sub.keywords}))
    return found


def _bare_in(source: str) -> dict:
    """{function: count} for every spawn in this source that passes no `env=`."""
    tree = ast.parse(source)
    roots, named = _roots(tree)
    owner: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                owner[sub] = node.name
    out: dict = {}
    for call, has_env in _spawns(tree, roots, named):
        if has_env:
            continue
        where = owner.get(call, "<module>")
        out[where] = out.get(where, 0) + 1
    return out


def _functions(rel: str) -> dict:
    """Every function in a file, by name, with its node and its source."""
    src = _src(rel)
    tree = ast.parse(src)
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = (node, ast.get_source_segment(src, node) or "")
    return out


# ---------------------------------------------------------------- the detector itself
# A gate is only worth the mistakes it can see, and this one has a shape it could miss: the
# three sites were found by a person reading rather than by a check, so the way a fourth
# would get in is by being spelled differently. The spellings are planted here and counted
# before the detector is trusted on the tree.
print("-- the detector, on the spellings that would get around it --")
_SELF = r'''
import subprocess
import subprocess as sp
import os
from subprocess import run as launch
from subprocess import Popen as open_it


def answer(a, env=None):
    return subprocess.run(a, env=env)


def through_the_module():
    return subprocess.run(["x"])


def through_an_alias():
    return sp.run(["x"])


def imported_by_name():
    return launch(["x"])


def imported_by_name_the_other_way():
    return open_it(["x"])


def through_the_shell():
    return os.system("x")


def quiet():
    return 1
'''
_self = _bare_in(_SELF)
_expected = {"through_the_module": 1, "through_an_alias": 1, "imported_by_name": 1,
             "imported_by_name_the_other_way": 1, "through_the_shell": 1}
check("every spelling that starts a process is counted, and only those",
      _self == _expected, f"got {_self}")
check("... a call that hands over an environment is not counted", "answer" not in _self,
      str(_self))
check("... and a function that starts nothing is not counted", "quiet" not in _self, str(_self))

# ---------------------------------------------------------------- the three sites
# Asked of the source by function, not by searching the file for a line: a rename or a moved
# call keeps a string search green, and that is the failure this file exists to stop.
print("\n-- the children that can run the app's code, and what they are given --")
_api_tree = ast.parse(_src("dashboard/plugin_api.py"))
_api_roots, _api_named = _roots(_api_tree)
_api = _functions("dashboard/plugin_api.py")

for fn, why in (("_app_menu", "it require()s the app's own launcher"),
                ("_venv_smoke", "it is the app's interpreter, and .pth files run at startup"),
                ("_pull_run", "it is a download driven by the app's own request")):
    node, src = _api[fn]
    spawns = _spawns(node, _api_roots, _api_named)
    check(f"{fn}: every child it starts is given an environment ({why})",
          bool(spawns) and all(has for _, has in spawns),
          f"{sum(1 for _, h in spawns if not h)} of {len(spawns)} spawn(s) inherit this process")
    check(f"... and that environment is the allowlist, not this process's",
          bool(src) and "child_env" in src)

check("_pull_run: the environment is allowlisted, not filtered by name",
      "os.environ" not in _api["_pull_run"][1])

_naming = _functions("dashboard/naming.py")["tracked_by_git"][1]
check("tracked_by_git: git runs inside someone's checkout on the allowlist",
      "subprocess.run" in _naming and "env=athena_engine.child_env()" in _naming)
check("... and when the allowlist cannot be reached, git is not run at all",
      _naming.index("import engine as athena_engine") < _naming.index("subprocess.run"))
check("_link_dir: the junction helper passes an environment too",
      "child_env" in _api["_link_dir"][1])

# ---------------------------------------------------------------- what it hands over
print("\n-- what a child is actually given --")
import engine as athena_engine  # noqa: E402  (after sys.path, deliberately)

# Planted, then looked for in the answer. Real names, because the rule is about names that
# exist rather than about a pattern: a denylist is exactly what would pass a shape test.
_SECRETS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "NOUS_API_KEY", "NOUS_PORTAL_KEY",
            "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
            "AWS_SECRET_ACCESS_KEY", "ATHENA_STORE", "ATHENA_STORE_TOKEN")
_saved = {k: os.environ.get(k) for k in _SECRETS}
try:
    for k in _SECRETS:
        os.environ[k] = "planted-value-that-must-not-travel"
    child = athena_engine.child_env()
    leaked = sorted(k for k in _SECRETS if k in child)
    check("none of this process's credentials reach a child", not leaked, f"leaked: {leaked}")
    check("... while a setting a child needs to work is still there",
          "PATH" not in os.environ or child.get("PATH") == os.environ.get("PATH"),
          "PATH was dropped")
    check("... and nothing arrives that the allowlist does not name",
          not (set(child) - set(athena_engine.CHILD_ENV_KEYS) - {"HF_HOME"}),
          f"extra keys: {sorted(set(child) - set(athena_engine.CHILD_ENV_KEYS))}")
    given = athena_engine.child_env({"HF_HOME": "/a/child/choice"})
    check("a coordinate a child needs is passed explicitly, and wins",
          given.get("HF_HOME") == "/a/child/choice", f"HF_HOME={given.get('HF_HOME')!r}")
    check("... and one passed as None is left out rather than sent as 'None'",
          "HF_HUB_OFFLINE" not in athena_engine.child_env({"HF_HUB_OFFLINE": None}))
    check("... and what it answers with is what a spawn accepts",
          all(isinstance(v, str) for v in child.values()))
finally:
    for k, was in _saved.items():
        if was is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = was

# ---------------------------------------------------------------- the class
# The counting rule, so the fourth site is a failure and not a discovery. Each entry is a
# spawn that inherits this process's environment on purpose, and each says why that is not
# the bug: none of them runs code that came from a cloned repository or from a person's
# file -- they run Athena's own tooling, or a package manager, on arguments Athena built.
DECLARED = {
    ("dashboard/engine.py", "_port_pids"): (2, "lsof/ss/netstat: this machine's own port table"),
    ("dashboard/engine.py", "_reap_port"): (1, "taskkill: Athena ends its own engine's tree, by pid"),
    ("dashboard/files.py", "videothumb"): (2, "ffprobe/ffmpeg: Athena's decoders, given a path"),
    ("dashboard/files.py", "_os_clipboard_files"): (1, "powershell: reads the Windows file clipboard"),
    ("dashboard/files.py", "copy_out"): (1, "powershell: writes the Windows file clipboard"),
    ("dashboard/idle.py", "users_on_port"): (1, "netstat: who holds a port on this machine"),
    ("dashboard/machines.py", "_pid_alive"): (1, "tasklist: is that pid still there"),
    ("dashboard/machines.py", "_run"): (1, "the guest runner: qemu-img/virsh on Athena's own argv"),
    ("dashboard/machines.py", "boot"): (1, "QEMU itself, on Athena's own argv, into its own log"),
    ("dashboard/machines.py", "grow_disk"): (1, "qemu-img on a disk Athena made"),
    ("dashboard/machines.py", "qemu_create_disk"): (1, "qemu-img on a disk Athena made"),
    ("dashboard/machines.py", "qemu_manifest"): (1, "winget show: a package manager asked about QEMU"),
    ("dashboard/machines.py", "qemu_stop"): (1, "taskkill on the guest's pid"),
    ("dashboard/node_runtime.py", "valid_node"): (1, "node -p: a version probe of a candidate"),
    ("dashboard/plugin_api.py", "_install"): (1, "MachineHost: a toolchain install, not app code"),
    ("dashboard/system.py", "_gpu_metrics"): (1, "nvidia-smi"),
    ("dashboard/system.py", "_gpu_procs"): (1, "nvidia-smi"),
}

print("\n-- every spawn that passes no env=, counted per function --")


def _bare() -> dict:
    out: dict = {}
    rels = ["dashboard/" + f for f in sorted(os.listdir(PLUGIN / "dashboard"))
            if f.endswith(".py")]
    rels.append("__init__.py")
    rels += ["tools/" + f for f in sorted(os.listdir(PLUGIN / "tools"))
             if f.endswith(".py")]
    for rel in rels:
        for fn, n in _bare_in(_src(rel)).items():
            out[(rel.replace("\\", "/"), fn)] = n
    return out


_b = _bare()
_new = sorted(f"{m}:{fn}" for m, fn in _b if (m, fn) not in DECLARED)
_gone = sorted(f"{m}:{fn}" for m, fn in DECLARED if (m, fn) not in _b)
_wrong = sorted(f"{m}:{fn} declared {DECLARED[(m, fn)][0]}, found {n}"
                for (m, fn), n in _b.items() if (m, fn) in DECLARED and n != DECLARED[(m, fn)][0])

check("no spawn outside the declared table inherits this process's environment",
      not _new, "; ".join(_new[:6]))
check("... and no entry has gone stale (a listed spawn that is gone fails too)",
      not _gone, "; ".join(_gone[:6]))
check("... and each is counted where the table says it is", not _wrong, "; ".join(_wrong[:4]))
check("... while the sites this round fixed are not in it at all",
      not any(fn in ("_app_menu", "_venv_smoke", "_pull_run", "tracked_by_git", "_link_dir")
              for _, fn in _b))

print(f"\nt_child_env: {PASS} passed, {FAIL} failed")
if FAILURES:
    for f in FAILURES:
        print(f"   - {f}")
sys.exit(1 if FAIL else 0)
