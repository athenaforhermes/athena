#!/usr/bin/env python3
"""The trust boundary, checked where it actually has teeth.

An installed Athena app is arbitrary code running as this person. That is the design, and it
belongs in `docs/threat-model.md` rather than in a hope. What is *tested* here are the places
where Athena constrains itself instead: the file browser's scope rule, the credential-store
refusal underneath it, the scan that may name a key but never print one, and the publish flow's
credential.

Three kinds of check, and the distinction matters:

  * **A refusal** — a path Athena must not hand over, asked for through the real resolver, with
    the reason in the answer. `..`, an absolute path outside every root, a symlinked shortcut to
    a credential store (the one that a naive `startswith` test would pass), `~/.ssh`.
  * **A non-refusal** — an app's own `.env` inside Athena's home is allowed, because the rule
    deliberately did not inherit filebox's "any file named .env" clause: an installed app
    legitimately owns one, and a scope rule that refuses the app's own config is a bug wearing a
    security costume. A guard that refuses everything is not a guard, it is a broken feature —
    so the negative cases are checked with the same care as the refusals.
  * **A redaction** — the credential scan may report a key's *name* and *location* and nothing
    else. The literal value is planted, then searched for in the whole serialized result, and a
    token-shaped run is searched for on top of that, because a scan that leaks is worse than a
    scan that finds nothing.

Usage: env -u PYTHONPATH -u ATHENA_HOME -u ATHENA_PORT <venv python> t_security.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")
PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")
sys.path.insert(0, str(PLUGIN / "dashboard"))
sys.path.insert(0, str(PLUGIN / "tools"))

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


def link_to(target: Path, at: Path) -> bool:
    """A directory link at `at` pointing at `target`, on whatever this OS will allow.

    Windows refuses `os.symlink` for directories unless the process holds SeCreateSymbolicLink
    (developer mode or an elevated shell), and the failure is an OSError, not a silent no-op —
    which is how a security test ends up *skipping* its own symlink checks and looking green.
    A junction needs no privilege at all and `Path.resolve()` walks through it, which is the
    property these checks turn on. So: try the symlink, fall back to a junction, and let the
    caller assert that a link exists rather than assuming one does.
    """
    try:
        os.symlink(target, at, target_is_directory=True)
        return True
    except OSError:
        pass
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(at), str(target)],
                           capture_output=True, text=True)
        return r.returncode == 0 and at.is_dir()
    return False


def refused(what, fn, *a, **kw) -> tuple[bool, str]:
    """True (and the reason) when the call raised the refusal, rather than returning cleanly.

    The refusal is a *raise*, not a return value — a dependency's return value is injected as a
    parameter and short-circuits nothing, so a route that "returned a 403" would look correct and
    enforce nothing. So the test has to look for the raise.
    """
    try:
        fn(*a, **kw)
        return False, f"not refused — {what} went through"
    except Exception as e:                                    # noqa: BLE001
        body = getattr(e, "detail", None) or str(e)
        return True, json.dumps(body) if isinstance(body, dict) else str(body)


print("== where a path may go ==")
import files  # noqa: E402

read_roots = files._read_roots()
write_roots = files._write_roots()
home = files._athena_home()
me = Path(os.path.realpath(Path.home()))

check("Athena's home is a read root", home in read_roots, str(read_roots))
check("... and so is every fixed disk", len(read_roots) >= 2, str(read_roots))
check("a write reaches Athena's home and nothing else, by default",
      write_roots == [home], str(write_roots))
check("... and no folder is a write root until a person names one",
      files._named_write_roots() == [], str(files._named_write_roots()))

# A file on a disk is readable: reads are deliberately wide, and pretending otherwise would
# make this a test of a rule Athena does not have.
check("a file in the user's own folder is inside the read scope",
      files._in_roots(me / "Documents" / "notes.txt", read_roots))

# The imaginary-drive case, because it is the cheapest real escape: a UNC path or a drive letter
# that is not a fixed disk resolves to nothing, and nothing is outside every root.
escaped, why = refused("a UNC path", files.enforce, [r"\\?\C:\Windows\System32\config\SAM"])
check("a path that resolves outside every root is refused on read", escaped, why)
check("... and the refusal says where Athena may look",
      escaped and files.REFUSED in why, why[:200])

escaped, why = refused("a write into Windows",
                       files.enforce, [str(Path(os.environ.get("SystemDrive", "C:") + "/Windows/Temp"))],
                       write=True)
check("a write outside Athena's home — and outside any folder you named — is refused",
      escaped, why)
check("... and that refusal says what the write scope is", "change" in why, why[:200])
check("while the same path is still readable, because reads are wide",
      not refused("a read of Windows", files.enforce,
                  [str(Path(os.environ.get("SystemDrive", "C:") + "/Windows/Temp"))])[0])

# `..` as text: the resolver is asked the same question a route would ask it.
walked = me / "Documents" / ".." / ".." / "Windows"
escaped, why = refused("a .. walk out of the write scope", files.enforce, [str(walked)], write=True)
check("a ..-walk out of the write scope is refused", escaped, why)

# The symlink is the case a string-prefix check gets wrong, so it is planted for real rather
# than simulated: a link *inside* an allowed root whose target is not.
tmp = Path(tempfile.mkdtemp(prefix="athena-sec-"))
outside = Path(os.environ.get("SystemDrive", "C:") + "/Windows")
link = tmp / "shortcut"
made_link = link_to(outside, link)
check("a link to Windows can actually be planted, so the escape below is a real escape",
      made_link, "this machine would not create a symlink or a junction")
check("a linked folder resolves outside the roots, so it is not 'inside' anything",
      not files._in_roots(link / "System32", write_roots)
      if made_link else False, "the link was not created")
if made_link:
    # The write roots are [home] — Athena's own home, and nothing else until a person names a
    # folder — so the scratch dir under the system temp is already out of scope, as is the
    # person's own folder. The interesting assertion is the one about a link planted where a
    # write IS allowed: it still cannot reach its target. The probe is therefore planted
    # inside Athena's home, the one folder a write may land in.
    in_scope_link = home / ".athena-sec-probe"
    try:
        link_to(outside, in_scope_link)
        check("a symlink planted inside an allowed root still cannot reach its target",
              not files._in_roots(in_scope_link / "System32", write_roots))
        escaped, why = refused("a write through a symlink",
                               files.enforce, [str(in_scope_link / "probe.txt")], write=True)
        check("... and a write through it is refused", escaped, why)
    finally:
        try:
            os.unlink(in_scope_link)
        except OSError:
            pass

print("\n== what a path may be, even when it is in scope ==")
# `~/.ssh` is inside the *read* scope — reads are deliberately wide, every fixed disk — so
# scope alone would hand over a private key.
reason = files._secret_reason(me / ".ssh" / "id_rsa")
check("a private key in the person's own folder is refused by the second question",
      reason == "~/.ssh", reason)
why_ssh = files._deny_secret(me / ".ssh" / "id_rsa")
check("... and a route answers with 403 for it, not a body",
      why_ssh is not None and why_ssh.status_code == 403,
      str(getattr(why_ssh, "status_code", None)))
escaped, why = refused("a read of ~/.ssh",
                       files.enforce, [str(me / ".ssh" / "id_rsa")])
check("... and the router floor raises for it wherever it sits", escaped, why)
check("... naming it as a credential store rather than as 'outside'",
      escaped and "credential" in why and files.REFUSED not in why, why[:240])

# Athena's own token directory is *inside* the home, which is otherwise allowed.
token_dir = home / "cache" / "HF_AUTH"
check("Athena's own token directory is refused even though it is inside its home",
      files._secret_reason(token_dir / "token") == "credential store",
      files._secret_reason(token_dir / "token"))
escaped, why = refused("a write into the token directory",
                       files.enforce, [str(token_dir / "token")], write=True)
check("... including a write", escaped, why)

# The same store, reached through a link with an innocent name: this is the symlink escape a
# name-based deny list misses, and the reason every check here realpaths first.
if made_link:
    innocent = tmp / "notes"
    try:
        link_to(token_dir, innocent)
        check("a symlink to the credential store is refused by its target, not its name",
              files._secret_reason(innocent / "token") == "credential store",
              files._secret_reason(innocent / "token"))
    except OSError as e:
        check("a symlink to the credential store is refused by its target, not its name",
              False, str(e))

# And the non-refusals, which are the half a guard is usually allowed to get wrong.
app_env = home / "api" / "some-app.git" / ".env"
check("an app's own .env inside Athena's home is allowed — the filebox rule was not inherited",
      files._secret_reason(app_env) == "", files._secret_reason(app_env))
check("an ordinary file in the person's folder is not called a credential",
      files._secret_reason(me / "Documents" / "taxes.txt") == "",
      files._secret_reason(me / "Documents" / "taxes.txt"))
check("... so it is not refused either",
      not refused("an ordinary file", files.enforce, [str(me / "Documents" / "taxes.txt")])[0])
# Hermes' own private data is a name outside the home, not a name inside it.
hermes_config = PLUGIN.parent.parent / "config.yaml"
check("Hermes' own config.yaml is refused by name, outside Athena's home",
      files._secret_reason(hermes_config) == "a file that holds credentials",
      files._secret_reason(hermes_config))

print("\n== a key may be named, never printed ==")
import keyscan  # noqa: E402

app = tmp / "api" / "some-app.git"
app.mkdir(parents=True)
SECRET = "sk-live-9f3ab77c2e51d4a8b6c0e14f7d2a5b93"
(app / ".env").write_text(
    "# what this app wants\n"
    "OPENAI_API_KEY=\n"
    "REPLICATE_API_TOKEN=\n"
    f"SOME_INTERNAL_TOKEN={SECRET}\n"
    "MODEL_DIR=/models\n"
    "PATH=/usr/bin\n", encoding="utf-8")
(app / "app.py").write_text(
    'import os\n'
    f'KEY = os.environ.get("ANTHROPIC_API_KEY", "{SECRET}")\n'
    'out = os.environ["MODEL_DIR"]\n', encoding="utf-8")

result = keyscan.scan(app, held=["openai"], folder_name="some-app")
blob = json.dumps(result)
rows = {k["name"]: k for k in result.get("keys") or []}
check("the scan finds the key an app asks for by name", "OPENAI_API_KEY" in rows, blob[:300])
check("... says where it is asked for, so the name has a location",
      (rows.get("OPENAI_API_KEY") or {}).get("where") == ".env",
      str(rows.get("OPENAI_API_KEY")))
check("... and marks a held credential as answered rather than missing",
      (rows.get("OPENAI_API_KEY") or {}).get("held") is True,
      str(rows.get("OPENAI_API_KEY")))
check("... with the unheld one still listed, because that is what the person has to act on",
      any(not r.get("held") for r in result.get("keys") or []), str(result.get("keys")))
check("the scan says how much of the folder it read, so 'none found' can be qualified",
      result.get("complete") is True and result.get("files_read", 0) >= 2, str(result)[:200])
check("a key named inside a script is found too",
      "ANTHROPIC_API_KEY" in blob, blob[:300])
check("THE VALUE IS NOWHERE IN THE RESULT — a scan that leaks is worse than one that finds none",
      SECRET not in blob, "the planted token appears in the scan's own output")
check("... not even a fragment of it",
      not any(SECRET[i:i + 16] in blob for i in range(0, len(SECRET) - 16, 4)),
      "a 16-character run of the token survived")
check("... and no token-shaped run survives anywhere in the evidence",
      keyscan._TOKEN_RUN.search(blob) is None or keyscan._TOKEN_RUN.search(blob).group(0).isupper(),
      str(keyscan._TOKEN_RUN.search(blob)))
check("a name that is not a credential is not reported as one", "MODEL_DIR" not in blob, blob[:300])
check("... neither is a plain PATH", '"PATH"' not in blob, blob[:300])

masked = keyscan._redact(f"SOME_INTERNAL_TOKEN={SECRET}", "SOME_INTERNAL_TOKEN")
check("the evidence line is masked", SECRET not in masked and "\u2026" in masked, masked)
check("... while the name it is about survives, because that is what the reader came for",
      "SOME_INTERNAL_TOKEN" in masked, masked)
check("a token is masked even when it is not the line's own name",
      SECRET not in keyscan._redact(f"client = OpenAI('{SECRET}')"), keyscan._redact(f"client = OpenAI('{SECRET}')"))

print("\n== an app id is a name, not a path ==")
# An id arrives from a URL, so `/apps/..` is reachable. `..` is a directory and it resolves to
# the Athena home — every app, every model cache, settings.json — and the uninstall route
# deletes what it resolves. That is the one traversal here with a delete on the end of it.
import importlib  # noqa: E402
_spec = importlib.util.spec_from_file_location("athena_sec_api", str(PLUGIN / "dashboard" / "plugin_api.py"))
api = importlib.util.module_from_spec(_spec)
sys.modules["athena_sec_api"] = api
_spec.loader.exec_module(api)

for bad in ("..", ".", "a/b", "a\\b", "", None, "C:evil"):
    check(f"the id {bad!r} does not name an app", not api._app_id_ok(bad), str(bad))
check("but an ordinary name does", api._app_id_ok("some-app.git"))
check("... and a dot inside a name is still just a name", api._app_id_ok("v1.2-app"))

fake_home = tmp / "athome"
(fake_home / "api" / "good-app").mkdir(parents=True)
(fake_home / "settings.json").write_text('{"port": 42001}', encoding="utf-8")
_real_home = api._athena_home
api._athena_home = lambda: fake_home
try:
    check("an id of .. resolves to no folder at all", api._app_dir("..") is None)
    res = api.app_uninstall("..")
    check("... so the delete route refuses it instead of deleting the install",
          getattr(res, "status_code", None) == 400, str(res)[:200])
    check("... and the home is still there afterwards",
          (fake_home / "settings.json").is_file(), str(fake_home))
    # A folder in api/ that is a link to somewhere else: the name check passes, so only the
    # resolve-time check is left to catch it.
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("not an app\n", encoding="utf-8")
    check("a link can be planted where an app folder would be",
          link_to(elsewhere, fake_home / "api" / "evil"), "no link was created")
    res2 = api.app_uninstall("evil")
    check("a symlinked app folder is refused as not installed",
          getattr(res2, "status_code", None) == 404, str(res2)[:200])
    check("... and what it pointed at is untouched",
          (elsewhere / "keep.txt").is_file())
    check("whether a folder is a direct child of api/ is decided by its resolved path",
          api._direct_child(elsewhere / "keep.txt", fake_home / "api") is False)
finally:
    api._athena_home = _real_home

print("\n== the publish flow's credential ==")
import publish_store as pub  # noqa: E402

check("no token means nothing is passed to git at all", pub.auth_args("") == [], str(pub.auth_args("")))
args = " ".join(pub.auth_args(SECRET))
check("with a token, git is told to read it from the environment",
      "ATHENA_GH_TOKEN" in args, args)
check("... and the token itself is not in the argument list — command-line config lands in the shell's history",
      SECRET not in args, args)

bare = tmp / "store.git"
apphome = tmp / "home" / "api" / "my-app.git"
apphome.mkdir(parents=True)
(apphome / "athena.js").write_text("// launcher\n", encoding="utf-8")
(pub.run(["git", "init", "-q", "--bare", str(bare)]) is not None)
out = tmp / "checkout"
# The checkout has to *have* an origin before `push` means anything: publish() writes into a
# checkout it is given, and a bare repo on this disk is the whole point — no network, and the
# push is still a real push with a real credential helper on the command line.
pub.run(["git", "init", "-q", "-b", "main", str(out)])
pub.run(["git", "-C", str(out), "remote", "add", "origin", str(bare)])
lines: list[str] = []
res = pub.publish(home=tmp / "home", out=out, repo="someone/athena-store",
                  push=True, token=SECRET, progress=lambda t: lines.append(str(t)))
log = "\n".join(lines)
check("a publish with a token pushes", res.get("pushed") is True, str(res)[:200])
check("THE TOKEN IS NOT IN THE JOB LOG the panel shows",
      SECRET not in log, "the token was echoed into the progress lines")
check("... and neither is any part of it",
      not any(SECRET[i:i + 16] in log for i in range(0, len(SECRET) - 16, 4)), log[-400:])
git_cfg = (out / ".git" / "config").read_text(encoding="utf-8", errors="replace")
check("nor is it written into the checkout's .git/config, which outlives the push",
      SECRET not in git_cfg, git_cfg[:300])
check("... while the credential helper it needs is still only on the command line",
      "x-access-token" not in git_cfg, git_cfg[:300])

shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{PASS}/{PASS + FAIL} checks passed")
if FAILURES:
    print("failing: " + "; ".join(FAILURES))
sys.exit(1 if FAIL else 0)
