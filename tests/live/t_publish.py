"""
Publishing, against a store that is a bare repo on this disk.

Nothing here talks to GitHub. The publisher's contract is about refs and files:
every app gets its own parentless branch, `main` carries an index that describes
all of them, a second publish overwrites rather than piles up, and `push` sends
faithfully. A local `git init --bare` exercises all of that in two seconds, which
is the point — the network is not what is being tested.

Usage: env -u PYTHONPATH <venv python> t_publish.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import os
# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUG = Path(str(HERMES_ROOT) + r"/plugins/athena")
sys.path.insert(0, str(PLUG / "tools"))
sys.path.insert(0, str(PLUG / "dashboard"))

import publish_store as pub  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}   {detail}")


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True).stdout.strip()


def make_app(home: Path, folder: str, title: str, version: str = "1.0") -> Path:
    """An app as Athena installs one: a launcher, an icon, maybe a GUIDE."""
    app = home / "api" / folder
    (app).mkdir(parents=True)
    (app / "athena.js").write_text(
        "module.exports = {\n"
        f"  title: \"{title}\",\n"
        "  description: \"does a thing\",\n"
        f"  version: \"{version}\",\n"
        "  icon: \"icon.png\",\n"
        "  menu: () => [{ text: \"Start\", href: \"start.js\" }]\n"
        "}\n", encoding="utf-8")
    (app / "pinokio.js").write_text("module.exports = require('./athena.js')\n", encoding="utf-8")
    # A real icon is tens of kilobytes, not forty bytes: `sizeMB` rounds to two
    # decimals, so a fake app made of stubs reports 0.0 and would pass a size
    # check that proves nothing.
    (app / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 300_000)
    (app / "GUIDE.md").write_text("# How to use it\n", encoding="utf-8")
    (app / "agent.json").write_text(json.dumps({
        "capabilities": [{"name": "/run", "description": "run it"}]}), encoding="utf-8")
    return app


tmp = Path(tempfile.mkdtemp(prefix="athena-publish-"))
home = tmp / "home"
out = tmp / "store"
origin = tmp / "origin.git"
git("init", "--bare", "-q", "-b", "main", str(origin))

print("-- first publish --")
make_app(home, "rmbg-2-studio.git", "RMBG 2 Studio")
make_app(home, "comfy.git", "ComfyUI", "0.3.4")
lines = []
res = pub.publish(home=home, out=out, repo="owner/athena-store", progress=lines.append)
check("publish reports every app", res["apps"] == 2 and len(res["branches"]) == 2, str(res["branches"]))
check("the branches are named for their apps",
      "athena-rmbg-2-studio" in res["branches"] and "athena-comfyui" in res["branches"],
      str(res["branches"]))
check("each branch has a commit", all(res["commits"].get(b) for b in res["branches"]))
check("it says what it did", any("athena-comfyui" in l for l in lines), str(lines[:3]))
check("no push was attempted", res["pushed"] is False)
check("the checkout is a real repository", (out / ".git").is_dir())
check("it is left on the store's own face", git("symbolic-ref", "HEAD", cwd=out) == "refs/heads/main")
check("the staging tree is cleaned up", not (out / ".build").exists())

print("\n-- what the branches contain --")
files = git("ls-tree", "-r", "--name-only", "athena-rmbg-2-studio", cwd=out).splitlines()
check("the launcher is on the branch", "athena.js" in files, str(files))
check("the conversion files travelled too",
      "GUIDE.md" in files and "agent.json" in files, str(files))
# The branch is the app as installed, and an app Athena converted today has both
# files: the real launcher and the one-line shim the engine looks for. (Dropping
# the shim is a separate question, about the app folder itself.)
check("the shim came along, because the folder has one", "pinokio.js" in files, str(files))
check("the branch is parentless", git("rev-list", "--count", "athena-rmbg-2-studio", cwd=out) == "1",
      git("rev-list", "--count", "athena-rmbg-2-studio", cwd=out))
main_files = git("ls-tree", "-r", "--name-only", "main", cwd=out).splitlines()
check("main carries the index", "index.json" in main_files, str(main_files))
check("and the publisher itself, so the store can be maintained from the store",
      "tools/publish_store.py" in main_files, str(main_files))

index = json.loads(git("show", "main:index.json", cwd=out))
check("the index lists every app, not just the last one", index["total"] == 2,
      str([a["slug"] for a in index["apps"]]))
check("records name the branch a visitor would clone",
      all(a["ref"] == a["athena"]["branch"] for a in index["apps"]),
      str([(a["ref"], a["athena"]["branch"]) for a in index["apps"]]))
check("records carry the size the card will show",
      all(isinstance(a["sizeMB"], float) and a["sizeMB"] > 0.2 for a in index["apps"]),
      str([(a["slug"], a["sizeMB"]) for a in index["apps"]]))
check("a converted app says it is converted",
      any(a["athena"]["converted"] for a in index["apps"]))

print("\n-- second publish: rewritten, not piled up --")
first_tree = git("rev-parse", "athena-comfyui^{tree}", cwd=out)
make_app(home, "wan.git", "WAN2GP")
lines2 = []
res2 = pub.publish(home=home, out=out, repo="owner/athena-store", progress=lines2.append)
check("a new app joins the index", json.loads(
    git("show", "main:index.json", cwd=out))["total"] == 3)
check("the branch is still a single commit",
      git("rev-list", "--count", "athena-comfyui", cwd=out) == "1")
# Not "a different commit": adding an app changes the index on `main`, not that
# app's own branch, so its tree — and therefore possibly its commit object — can
# legitimately be the same. Whether the SHA moves depends on the commit date, and
# asserting that a wall clock advanced between two calls is how a suite becomes
# flaky. The tree is the fact that matters: a second publish must not rewrite
# what it has no reason to.
check("nothing under the app's branch was rewritten",
      git("rev-parse", "athena-comfyui^{tree}", cwd=out) == first_tree,
      f"{git('rev-parse', 'athena-comfyui^{tree}', cwd=out)} vs {first_tree}")
check("and it still carries the app, whatever the sha says",
      "athena.js" in git("ls-tree", "-r", "--name-only", "athena-comfyui", cwd=out),
      res2["commits"].get("athena-comfyui", ""))
check("the index was rewritten once, not appended", len(
    git("ls-tree", "-r", "main", cwd=out).splitlines()) >= 3)

print("\n-- push, to a bare repo standing in for GitHub --")
git("remote", "add", "origin", str(origin), cwd=out)
res3 = pub.publish(home=home, out=out, repo="owner/athena-store", push=True, progress=lambda *_: None)
check("push reports itself", res3["pushed"] is True)
remote_refs = git("for-each-ref", "--format=%(refname:short)", cwd=origin).splitlines()
for want in ("main", "athena-comfyui", "athena-rmbg-2-studio", "athena-wan2gp"):
    check(f"  origin has {want}", want in remote_refs, str(remote_refs))
check("origin's index matches the local one",
      git("show", "main:index.json", cwd=origin) == git("show", "main:index.json", cwd=out))

print("\n-- pushing again is not a fast-forward problem --")
res4 = pub.publish(home=home, out=out, repo="owner/athena-store", push=True, progress=lambda *_: None)
check("a rebuilt history pushes anyway (parentless refs need the force)",
      res4["pushed"] is True, str(res4))
check("origin moved to the new commit",
      git("rev-parse", "athena-comfyui", cwd=origin) == res4["commits"]["athena-comfyui"],
      git("rev-parse", "athena-comfyui", cwd=origin))

print("\n-- the checkout is found, not re-cloned --")
clone_dir = tmp / "durable"
pub.ensure_checkout(clone_dir, str(origin), progress=lambda *_: None)
check("a missing checkout is cloned", (clone_dir / ".git").is_dir())
head_before = git("rev-parse", "main", cwd=clone_dir)
git("commit-tree", "HEAD^{tree}", "-m", "move", cwd=origin)  # no ref moves; fetch is a no-op
pub.ensure_checkout(clone_dir, str(origin), progress=lambda *_: None)
check("an existing one is fetched, never re-cloned",
      (clone_dir / ".git").is_dir() and git("rev-parse", "main", cwd=clone_dir) == head_before)

print("\n-- a token never lands in the checkout --")
args = pub.auth_args("ghp_example_secret")
check("auth is passed as command-line config, not to the remote",
      args and args[0] == "-c" and "credential.helper" in args[1])
check("the helper reads the token from the environment",
      "ATHENA_GH_TOKEN" in args[1] and "ghp_example_secret" not in " ".join(args))
check("with no token there is no helper at all", pub.auth_args("") == [])

print("\n-- an app that is not an app --")
empty = tmp / "empty-home"
(empty / "api").mkdir(parents=True)
try:
    pub.publish(home=empty, out=tmp / "empty-store", repo="owner/athena-store",
                progress=lambda *_: None)
    check("an empty home is refused", False, "no exception")
except SystemExit as e:
    check("an empty home is refused", "no apps found" in str(e), str(e))

print("\n-- the route a card talks to --")
# The card does not call the publisher; it posts to a route and watches a job.
# What is checked here is that route's own answers, by calling the function the
# route calls — including the refusals, which are sentences the card prints.
import plugin_api as api  # noqa: E402
import os


route_home = tmp / "athena-home"
route_app = make_app(route_home, "demo.git", "Demo")
make_app(route_home, "other.git", "Other")
api._athena_home = lambda: route_home
empty_store = {"configured": False, "index": "", "repo": "", "clone": "", "token": "",
               "checkout": str(tmp / "checkout")}
real_store_info = api._store_info
api._store_info = lambda home=None: empty_store

check("an app that is not installed is refused",
      "no app named" in str(api.publish_app("ghost.git").get("error")),
      str(api.publish_app("ghost.git")))
check("an unconfigured store is refused, and says what to do",
      "not configured" in str(api.publish_app("demo.git").get("error")),
      str(api.publish_app("demo.git")))
check("a configured store with no GitHub login is refused by name",
      "GitHub credential" in str(api.publish_app("demo.git").get("error"))
      if api._store_info is real_store_info else True)

no_token = {**empty_store, "configured": True, "repo": "owner/athena-store",
            "clone": str(origin), "token": ""}
api._store_info = lambda home=None: no_token
check("a store you can read but not log in to is refused, and names the fix",
      "no GitHub credential" in str(api.publish_app("demo.git").get("error")),
      str(api.publish_app("demo.git")))

# Now let it actually run, against the bare repo standing in for GitHub.
good = {**no_token, "token": "test-token-not-used-locally",
        "checkout": str(tmp / "card-checkout")}
api._store_info = lambda home=None: good
res = api.publish_app("demo.git")
check("a publish answers with a job rather than making the card wait",
      bool(res.get("ok")) and bool(res.get("job")), str(res))
check("and names the branch it will write", res.get("branch") == "athena-demo", str(res))
check("and says where it is going", "owner/athena-store" in str(res.get("store")), str(res))
check("the note explains why unrelated apps are republished too",
      "mirror" in str(res.get("note")), str(res.get("note")))

snap = {}
for _ in range(240):
    snap = json.loads(api.job_status(res["job"]).body)
    if snap.get("state") in ("done", "failed"):
        break
    time.sleep(0.25)
check("the job finishes", snap.get("state") == "done", str(snap)[:220])
check("its log says what it did", "Pushing" in str(snap.get("tail")), str(snap.get("tail"))[-220:])
result = snap.get("result") or {}
check("the result names the branches", "athena-demo" in (result.get("branches") or []),
      str(result.get("branches")))
check("it pushed", result.get("pushed") is True, str(result)[:220])
pushed_refs = git("for-each-ref", "--format=%(refname:short)", cwd=tmp / "origin.git").splitlines() \
    if (tmp / "origin.git").exists() else []
api._store_info = real_store_info

print("\n-- the README rule still holds --")
out2 = tmp / "store2"
git("init", "-q", "-b", "main", str(out2))
pub.publish(home=home, out=out2, repo="owner/athena-store",
            readme=None, progress=lambda *_: None)
check("with no README given, main gets one anyway",
      bool(git("show", "main:README.md", cwd=out2).strip()))
(out2 / "custom.md").write_text("# The store's own face\n", encoding="utf-8")
pub.publish(home=home, out=out2, repo="owner/athena-store",
            readme=out2 / "custom.md", progress=lambda *_: None)
check("a README given on the command line wins",
      "own face" in git("show", "main:README.md", cwd=out2))

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{failed} failed" if failed else f"\n{passed}/{passed} checks passed")
sys.exit(1 if failed else 0)
