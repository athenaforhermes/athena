#!/usr/bin/env python3
"""Publish Athena's installed apps into the Athena Store repository.

The store is one repository whose `main` branch is the index and whose other
branches are the apps: one branch per app, holding that app's own launcher files
at the root. That shape is deliberate. An installed app is a `git clone` of its
launcher repository and the app's own `update.js` runs `git pull` at its root —
so an app that arrived as a *folder* of some other repository has no history to
pull and its Update button is dead on arrival. A branch is a real ref: clone the
branch and the folder is a genuine checkout, pull and it brings the app's next
version.

Usage (from anywhere):

    python publish_store.py --home <athena home> --out <store repo dir> \
                            --repo <owner/name> [--only slug,slug]

`--home` is Athena's home (the folder holding `api/`); each child of `api/` that
looks like an app is published. Re-running is safe: branches are rewritten with
the installed content, so the store tracks the machine.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from pathlib import Path

# Athena's own names live with the rest of the backend; this tool is also run
# from the command line, so it finds them by path rather than by package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import naming as athena_naming  # noqa: E402

# Files Athena's conversion adds to an app. They are what makes the app usable by
# an agent, so they belong in the store copy even though the app's own repository
# has never heard of them.
CONVERSION_FILES = ("GUIDE.md", "agent.json")

# A human-facing slug per app. Anything not listed falls back to a slug derived
# from the folder name, so a new app needs no edit here.
SLUGS = {
    "comfy": ("athena-comfyui", ["image", "video", "workflow", "comfyui"]),
    "hunyuan3d-2-lowvram": ("athena-hunyuan3d-2-lowvram", ["3d", "image-to-3d", "text-to-3d"]),
    "maestro": ("athena-maestro", ["image", "video", "audio", "editor", "studio"]),
    "rmbg-2-studio": ("athena-rmbg-2-studio", ["image", "background-removal", "matting"]),
    "wan": ("athena-wan2gp", ["video", "image", "audio", "wan"]),
    "yue2-groove-pinokio": ("athena-yue2-groove", ["music", "audio", "song", "text-to-music"]),
}


# Endpoint names that are a widget handler rather than a capability, mirroring
# Athena's own rule: `/_on_remove` is "the button was clicked", not "remove
# something", and the count in the store should be the count of things an agent
# can actually ask for.
PLUMBING = re.compile(r"^(?:_|on_[a-z_]+$|update_preview(_\d+)?$|lambda(_\d+)?$|reset_?.*$|open_output_folder(_\d+)?$|refresh.*$)")


def run(cmd, cwd=None, check=True, env=None):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} failed ({p.returncode}):\n{(p.stdout + p.stderr)[-2000:]}")
    return (p.stdout or "") + (p.stderr or "")


def git(cwd, *args, check=True, env=None):
    """Run git in a directory. `env` is the caller's environment, not a suggestion.

    It was accepted and then dropped, so every caller that built an environment for a git
    command -- the GitHub token, `GIT_TERMINAL_PROMPT=0` -- ran without it: `git fetch` over
    https asking a terminal that is not there, and a private repo answering 404 instead of
    authenticating. A parameter that is not forwarded is worse than no parameter, because the
    caller reads the call site and believes it.
    """
    return run(["git", "-C", str(cwd), *args], check=check, env=env)


def ensure_identity(repo_dir: Path) -> None:
    """Give the repo a committer if git has none.

    Hermes is logged in to GitHub but its git has no global identity, and
    `git commit` refuses outright without one. Attribute the commit to the
    logged-in account using GitHub's own noreply address, so the history shows
    the user rather than the machine.
    """
    if git(repo_dir, "config", "user.email", check=False).strip():
        return
    login, uid = "", ""
    try:
        login = run(["gh", "api", "user", "--jq", ".login"]).strip() or login
        uid = run(["gh", "api", "user", "--jq", ".id"]).strip()
    except SystemExit:
        pass
    if not login:
        # No `gh` here. A local identity rather than somebody else's GitHub handle
        # written into the source: this file ships, and the name in it would then
        # be a claim about whoever ran it.
        git(repo_dir, "config", "user.name", os.environ.get("GIT_AUTHOR_NAME") or "athena-store")
        git(repo_dir, "config", "user.email",
            os.environ.get("GIT_AUTHOR_EMAIL") or "athena-store@localhost")
        return
    git(repo_dir, "config", "user.name", login)
    git(repo_dir, "config", "user.email",
        f"{uid}+{login}@users.noreply.github.com" if uid else f"{login}@users.noreply.github.com")


def field(js: str, name: str):
    m = re.search(rf'{name}\s*:\s*"((?:[^"\\]|\\.)*)"', js, re.S)
    if not m:
        return None
    return m.group(1).encode().decode("unicode_escape")


def debrand(text: str) -> str:
    """Remove the vendor name from an app's own words.

    Descriptions written for the other store often carry its name. Athena's
    store is its own front end and does not advertise a competitor in its
    listings; the app itself is untouched.
    """
    if not text:
        return text
    text = re.sub(r"\bPinokio('s)?\b", "", text)
    text = re.sub(r"\bpinokio\b", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" -|")


def app_files(app: Path) -> list[str]:
    """The files worth publishing: everything git tracks, plus conversion output.

    Tracked means "the app's repository says this is part of the app" — it
    excludes the built `app/` checkout, virtualenvs, logs, model caches and
    generated outputs, all of which are untracked and many of which are tens of
    gigabytes. Falls back to a name filter for a folder that is not a checkout.

    A folder can disappear between being listed and being read — a person
    uninstalling an app while the panel refreshes is the ordinary case, not a
    corrupt one. There is nothing to publish from a folder that is gone, so that
    answers an empty list rather than raising: a store scan must report the apps
    it could read, not die on the one that left.
    """
    tracked = git(app, "ls-files", "-z", check=False)
    # `git()` merges stderr into its answer (callers rely on that to spot a
    # missing ref), so a folder that is not a checkout comes back as the word
    # "fatal:" — which is not a file, and which used to make this list non-empty
    # and skip the fallback below. A folder of launcher files would then publish
    # as a branch containing nothing but GUIDE.md.
    if tracked.lstrip().startswith(("fatal:", "error:")):
        tracked = ""
    files = [f for f in tracked.split("\0") if f]
    if not files:
        keep = re.compile(r"\.(js|json|html|md|txt|png|jpe?g|webp)$", re.I)
        try:
            entries = list(app.iterdir())
        except OSError:
            return []
        files = [str(p.relative_to(app)).replace("\\", "/")
                 for p in entries if p.is_file() and keep.search(p.name)]
    have = set(files)
    for extra in CONVERSION_FILES:
        if (app / extra).is_file() and extra not in have:
            files.append(extra)
    return sorted(files)


def describe(app: Path, slug: str, name: str, repo: str, files: list[str]) -> dict:
    lp = athena_naming.launcher_of(app)
    try:
        js = lp.read_text(encoding="utf-8", errors="replace") if lp else ""
    except OSError:
        # The launcher was there when it was found and is gone now: the folder
        # is being removed under this scan. Describe it as an app with no
        # launcher text, which is honest, instead of raising on a dead path.
        js = ""
    title = field(js, "title") or app.name.replace(".git", "")
    desc = debrand(field(js, "description") or "")
    version = field(js, "version") or "1.0"
    icon = field(js, "icon")
    origin = git(app, "remote", "get-url", "origin", check=False).strip()
    caps = []
    aj = app / "agent.json"
    if aj.is_file():
        try:
            caps = json.loads(aj.read_text(encoding="utf-8")).get("capabilities") or []
        except Exception:  # noqa: BLE001
            caps = []
    caps = [c for c in caps if not PLUMBING.match(str((c or {}).get("name") or "").lstrip("/"))]
    size = 0
    for f in files:
        try:
            size += (app / f).stat().st_size
        except OSError:
            pass
    branch = slug
    raw = f"https://raw.githubusercontent.com/{repo}/{branch}"
    rec = {
        "slug": slug,
        "title": title,
        "description": desc,
        "version": version,
        "listed": True,
        "status": "active",
        "repoUrl": origin or None,
        "ref": branch,
        "install": f"https://github.com/{repo}#{branch}",
        "icon": icon,
        "iconUrl": f"{raw}/{icon}" if icon else None,
        "tags": [{"tag": t, "count": 0} for t in tags_for(app.name)],
        "sizeMB": round(size / 1e6, 2),
        "athena": {
            "branch": branch,
            "guide": "GUIDE.md" if (app / "GUIDE.md").is_file() else None,
            "agent": "agent.json" if (app / "agent.json").is_file() else None,
            "capabilities": len(caps),
            "converted": bool(caps),
        },
    }
    return rec


def tags_for(folder: str) -> list[str]:
    key = folder[:-4].lower() if folder.lower().endswith(".git") else folder.lower()
    return SLUGS.get(key, (None, ["ai"]))[1]


def slug_for(folder: str) -> str:
    key = folder[:-4].lower() if folder.lower().endswith(".git") else folder.lower()
    if key in SLUGS:
        return SLUGS[key][0]
    return "athena-" + re.sub(r"[^a-z0-9]+", "-", key).strip("-")


def apps_in(home: Path) -> list[Path]:
    """Every installed app under `home/api`, skipping one that vanishes.

    The listing and the check are two different reads of the filesystem, and
    between them a person can uninstall an app: the folder is in the listing and
    gone by the time it is looked at. That is a normal event, not a fault, so an
    entry that cannot be read is skipped rather than allowed to abort the scan.
    """
    api = home / "api"
    try:
        entries = list(api.iterdir())
    except OSError:
        return []
    found = []
    for p in entries:
        try:
            if p.is_dir() and athena_naming.has_launcher(p):
                found.append(p)
        except OSError:
            continue
    return sorted(found)


def write_branch(repo_dir: Path, name: str, files: Dict[str, bytes], message: str,
                 stage_root: Path) -> str:
    """Write one branch as a single parentless commit, built from a staging tree.

    This uses plumbing (`write-tree` + `commit-tree` + `update-ref`) rather than
    `git checkout --orphan`, and that is the whole point. `--orphan` **fails the
    moment the branch already exists**, and with `check=False` the failure is
    silent: git stays on the previous branch, so the next commit lands on the
    wrong branch. That is exactly how the store's README once came to be an
    app's README — the app's files were committed onto `main`, and `main` then
    carried the app's README forward. Plumbing has no current branch to be
    wrong about: each ref is written to directly, from a tree, with no parent.

    Nothing is ever deleted from the repository directory either. A staging tree
    under `.build/` is added to a throwaway index, so the store's own files are
    never the thing being rebuilt.
    """
    stage = stage_root / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for rel, blob in files.items():
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(blob)

    index = repo_dir / ".git" / f"index-{name}"
    index.unlink(missing_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index), "GIT_WORK_TREE": str(stage)}
    run(["git", "add", "-A", "."], cwd=str(stage), env=env)
    tree = run(["git", "write-tree"], cwd=str(stage), env=env).strip()
    commit = run(["git", "commit-tree", tree, "-m", message], cwd=str(stage), env=env).strip()
    run(["git", "update-ref", f"refs/heads/{name}", commit], cwd=str(repo_dir))
    index.unlink(missing_ok=True)
    return commit


def branch_readme(rec: dict) -> str:
    """What this branch is, in Athena's words.

    The app's own README documents installing with the tooling Athena replaces,
    and that text is the project's writing and its provenance — so it stays in
    the branch, moved aside as `README.upstream.md`. It just is not what the
    branch leads with.
    """
    lines = [f"# {rec['title']}", ""]
    desc = (rec.get("description") or "").strip()
    if desc:
        lines += [desc, ""]
    if rec.get("repoUrl"):
        lines.append(f"- Upstream project: {rec['repoUrl']}")
    lines.append(f"- Athena Store branch: `{rec['athena']['branch']}`")
    if rec.get("version"):
        lines.append(f"- Version: {rec['version']}")
    lines += [
        "",
        "This branch is the app as installed in Athena: its launcher files, plus the",
        "conversion output Athena writes beside them (`GUIDE.md`, `agent.json`) once the",
        "app has been converted. Athena reads the launcher from here, runs its install",
        "script, and starts the app from this branch's own copy.",
        "",
        "The application's own documentation, as written by the project, is kept in",
        "`README.upstream.md`.",
        "",
    ]
    return "\n".join(lines)


def publish_one(repo_dir: Path, app: Path, rec: dict, files: list[str],
                stage_root: Path) -> None:
    """One app, one branch, one commit: the app's files as installed here."""
    blobs: Dict[str, bytes] = {}
    for rel in files:
        src = app / rel
        if src.is_file():
            blobs[rel.replace("\\", "/")] = src.read_bytes()
    prose = branch_readme(rec).encode("utf-8")
    own = blobs.pop("README.md", None)
    if "README.upstream.md" not in blobs:
        if own is not None and own.strip() != prose.strip():
            blobs["README.upstream.md"] = own
        else:
            # It was moved aside on an earlier publish, and the app folder has it
            # under its original name only. Carry the copy across, or every
            # republish would quietly delete the project's own writing.
            prev = git(repo_dir, "show", f"{rec['athena']['branch']}:README.upstream.md",
                       check=False)
            if prev.strip() and not prev.lstrip().startswith(("fatal:", "error:")):
                blobs["README.upstream.md"] = prev.encode("utf-8")
    blobs["README.md"] = prose
    return write_branch(repo_dir, rec["athena"]["branch"], blobs,
                 f"{rec['title']} {rec['version']} — Athena Store copy\n\n"
                 f"Files as installed in Athena, plus Athena's own conversion output\n"
                 f"(GUIDE.md, agent.json) when the app has been converted.\n"
                 f"Upstream: {rec['repoUrl'] or 'unknown'}\n",
                 stage_root)


def auth_args(token: str) -> list:
    """Command-line config that lets git use Hermes' own GitHub login.

    The token travels in the environment, not in the remote URL and not in
    `-c credential.helper=...` written after it: command-line config is used for
    one invocation and never lands in the checkout's `.git/config`, so a durable
    store clone does not become a file with a secret in it.
    """
    if not token:
        return []
    return ["-c", "credential.helper=!f() { echo username=x-access-token; "
                  "echo password=$ATHENA_GH_TOKEN; }; f"]


def ensure_checkout(out: Path, clone_url: str, token: str = "", progress=print) -> Path:
    """The durable store checkout: cloned once, fetched after that.

    It lives outside any scratch folder on purpose. The store is what the Store
    tab reads and what a publish writes, and a copy that a cache pruner can
    delete is a copy that will be deleted at the worst moment.
    """
    out = Path(out)
    if (out / ".git").is_dir():
        progress(f"Fetching {clone_url}")
        git(out, *auth_args(token), "fetch", "--all", "--prune", check=False,
            env={**os.environ, "ATHENA_GH_TOKEN": token} if token else None)
    else:
        progress(f"Cloning {clone_url}")
        run(["git", *auth_args(token), "clone", "-q", clone_url, str(out)],
            env={**os.environ, "ATHENA_GH_TOKEN": token} if token else None)
    return out


def publish(home: Path, out: Path, repo: str, readme=None, push: bool = False,
            token: str = "", progress=print) -> Dict[str, Any]:
    """Publish `<home>/api/*` into the store checkout at `out`.

    Every app is rewritten on every publish, and that is deliberate rather than
    lazy: the store is this machine's mirror, and an index that describes apps
    whose branches were not rebuilt is an index that lies about what a visitor
    will download. The whole job is small — launcher files, not models — so the
    cost of being right is a few seconds.

    `push` sends the refs to `origin`. The branches are single parentless
    commits rebuilt from scratch, so a push is a force by definition; there is
    one owner and one writer, which is what makes that safe.
    """
    home = Path(home)
    repo_dir = Path(out)
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").is_dir():
        run(["git", "init", "-q", "-b", "main", str(repo_dir)])
    ensure_identity(repo_dir)
    # Byte-exact copies, not line-ending-normalised ones: these branches are the
    # apps as installed, and a launcher's bytes are part of what it is.
    git(repo_dir, "config", "core.autocrlf", "false", check=False)
    stage_root = repo_dir / ".build"
    stage_root.mkdir(exist_ok=True)

    apps = apps_in(home)
    if not apps:
        raise SystemExit("no apps found under <home>/api")

    records, written, commits = [], [], {}
    for app in apps:
        # Between `apps_in` and here, a person can uninstall the app. There is
        # nothing to publish from a folder that is gone, and an empty branch is
        # worse than no branch: it would tell the store this app exists and has
        # no files. Skip it and say so.
        if not app.is_dir():
            progress(f"  {app.name}: removed during the scan, skipped")
            continue
        # The slug is the branch name, and a slug with "athena-" in it is what
        # makes a store branch tell the truth about whose app it is.
        slug = slug_for(app.name)
        files = app_files(app)
        rec = describe(app, slug, app.name, repo, files)
        rec["id"] = len(records) + 1
        progress(f"  {slug:34s} {len(files):5d} files {rec['sizeMB']:8.2f} MB  "
                 f"{'converted' if rec['athena']['converted'] else 'raw'}")
        records.append(rec)
        commits[slug] = publish_one(repo_dir, app, rec, files, stage_root)
        written.append(slug)

    index = {
        "store": "Athena Store",
        "version": 1,
        "repo": f"https://github.com/{repo}" if repo else "",
        "layout": "one branch per app; this file is the index on main",
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": len(records),
        "apps": records,
    }

    write_main(repo_dir, index, stage_root,
               readme=Path(readme) if readme else None)
    shutil.rmtree(stage_root, ignore_errors=True)
    commits["main"] = git(repo_dir, "rev-parse", "refs/heads/main").strip()
    # Leave the checkout on the store's own face, not on whichever app was last.
    git(repo_dir, "symbolic-ref", "HEAD", "refs/heads/main", check=False)
    git(repo_dir, "reset", "-q", "--hard", "main", check=False)
    progress(f"index.json: {len(records)} apps -> {repo_dir / 'index.json'}")

    pushed = False
    if push:
        refs = ["main"] + written
        progress(f"Pushing {len(refs)} refs to origin")
        env = {**os.environ, "ATHENA_GH_TOKEN": token} if token else None
        git(repo_dir, *auth_args(token), "push", "--force", "origin", *refs, env=env)
        pushed = True

    return {"ok": True, "apps": len(records), "branches": written,
            "commits": commits, "repo": repo, "checkout": str(repo_dir),
            "index": str(repo_dir / "index.json"), "pushed": pushed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", required=True, help="owner/name the branches will be pushed to")
    ap.add_argument("--readme", default="", help="README to put on main when the repo has none yet")
    ap.add_argument("--push", action="store_true", help="push main and every app branch")
    ap.add_argument("--token", default="", help="GitHub token (or use $ATHENA_GH_TOKEN)")
    ap.add_argument("--clone-url", default="", help="clone this instead of the checkout's origin")
    args = ap.parse_args()

    out = Path(args.out)
    token = args.token or os.environ.get("ATHENA_GH_TOKEN", "")
    if args.clone_url:
        ensure_checkout(out, args.clone_url, token)
    publish(home=Path(args.home), out=out, repo=args.repo,
            readme=args.readme or None, push=args.push, token=token)
    return 0


def write_main(repo_dir: Path, index: Dict[str, Any], stage_root: Path,
               readme: Optional[Path] = None) -> None:
    """Rebuild `main`: the index, the README, and this script.

    `main` is the store's face — what the repository page shows and what the
    catalogue reads. The README given on the command line always wins; only when
    none is given is the one already on `main` carried forward, so re-publishing
    an app never wipes the store's own writing. (Carrying it *unconditionally*
    is how an app's README once took over this branch.)
    """
    carried: Optional[bytes] = None
    if readme is None:
        blob = git(repo_dir, "show", "main:README.md", check=False)
        if blob.strip() and not blob.lstrip().startswith(("fatal:", "error:")) and len(blob) > 40:
            carried = blob.encode("utf-8")

    if readme is not None and readme.is_file():
        prose = readme.read_bytes()
    elif carried is not None:
        prose = carried
    else:
        prose = b"# Athena Store\n"

    files = {
        "index.json": (json.dumps(index, indent=2) + "\n").encode("utf-8"),
        "README.md": prose,
        "tools/publish_store.py": Path(__file__).read_bytes(),
    }
    write_branch(repo_dir, "main", files,
                 f"Athena Store index\n\n{len(index['apps'])} apps, published {index['updated']}.",
                 stage_root)


if __name__ == "__main__":
    sys.exit(main())
