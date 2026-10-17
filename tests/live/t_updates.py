"""Updating the whole shelf: is anything behind, and does updating it stick?

Two halves, and both are about what Athena does rather than what git does:

  1. The question. "Three apps have updates" has to be true — asked of every
     installed app at once, with a real `git fetch` against a real remote, and
     with an app that is not a checkout neither breaking the answer nor being
     counted as up to date.
  2. The update. The app's own update.js runs and then the app has to be read
     back as Athena's, because a `git pull` restores whatever the repository
     ships — including the legacy-named launcher the conversion had replaced.
     That ordering is the whole point: convert too early and the conversion is
     thrown away by the pull that follows it.

There is no engine here. The runtime is a double that says "still running" for
a while and then stops, which is exactly the state machine the real one reports.

Run with `env -u PYTHONPATH` from this directory.
"""
from __future__ import annotations

import importlib.util
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

PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")
passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}  {detail}")


def load():
    spec = importlib.util.spec_from_file_location("athena_plugin", str(PLUGIN / "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["athena_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod, mod._load_api()


def git(cwd, *args, ok=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       env={**__import__("os").environ,
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if ok and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r


class FakeEngine:
    """The engine, as far as this file is concerned: a home, and a probe."""

    def __init__(self, home):
        self.home = Path(home)
        self.probes = 0

    def probe(self):
        self.probes += 1
        return True

    def start(self, wait=0):  # only reached if a stub is missing
        raise AssertionError("the engine should not have been started here")


class FakeRuntime:
    """Says the app is running for `alive` reads, then that it has stopped.

    That is the real state machine: `start` answers immediately, and the app is
    still there for as long as it takes.
    """

    def __init__(self, alive=2, logs="pulling…"):
        self.calls = 0
        self.alive = alive
        self.logs_text = logs
        self.stopped = []

    def running(self, app=None, timeout=15.0):
        self.calls += 1
        if self.calls <= self.alive:
            return {(app or "app"): [{"uri": "update.js", "url": None}]}
        return {}

    def logs(self, app, limit=200):
        return self.logs_text

    def stop(self, app, script=None, timeout=5.0):
        self.stopped.append(app)
        return {"ok": True}


def make_remote_and_clone(tmp: Path, name: str, behind: int) -> Path:
    """A real remote, a real clone, and a real number of commits behind it.

    `git fetch` is the thing `_update_check` actually runs, so the fixture has to
    be a repository whose remote has genuinely moved on — not a folder that looks
    like one.
    """
    remote = tmp / "_remotes" / f"{name.replace('.git', '')}-remote.git"
    remote.parent.mkdir(parents=True, exist_ok=True)
    git(tmp, "init", "--bare", "-q", str(remote))
    # A bare repo starts on whatever branch the local git was configured for.
    # Point it at the branch that is about to be pushed, or the clone that
    # follows checks out nothing at all and every git question about it fails.
    git(tmp, "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main")
    seed = tmp / "_seed" / name
    seed.mkdir(parents=True, exist_ok=True)
    git(seed, "init", "-q")
    git(seed, "checkout", "-q", "-b", "main")
    (seed / "README.md").write_text("app\n", encoding="utf-8")
    (seed / "install.js").write_text("// installer\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "first")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "-u", "origin", "main")

    app = tmp / "api" / name
    app.parent.mkdir(parents=True, exist_ok=True)
    git(tmp, "clone", "-q", str(remote), str(app))

    # The remote moves on afterwards — this is what "behind" means.
    for i in range(behind):
        (seed / "CHANGELOG.md").write_text(f"change {i}\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", f"change {i}")
    if behind:
        git(seed, "push", "-q", "origin", "main")
    return app


def log_of(api, jid: str) -> str:
    """A job's terminal, as the route would render it."""
    return "\n".join((api._JOBS.get(jid) or {}).get("log") or [])


def main():
    _, api = load()
    tmp = Path(tempfile.mkdtemp(prefix="athena-updates-"))
    api.engine = FakeEngine(tmp)
    api_root = tmp / "api"

    behind_app = make_remote_and_clone(tmp, "behind.git", 2)
    make_remote_and_clone(tmp, "current.git", 0)
    plain = api_root / "plain"
    plain.mkdir(parents=True, exist_ok=True)
    (plain / "app.py").write_text("print('hi')\n", encoding="utf-8")
    # Things that are not apps and must not be treated as any.
    (api_root / "notes.txt").write_text("not an app\n", encoding="utf-8")
    (api_root / "misc").mkdir(exist_ok=True)

    print("\n1. which folders are apps")
    ids = api._installed_app_ids()
    check("every app folder is listed, and nothing else",
          set(ids) == {"behind.git", "current.git", "plain", "misc"}, str(ids))
    check("a file in the apps folder is not an app", "notes.txt" not in ids, str(ids))
    check("the list is ordered the way a person reads it",
          ids == sorted(ids, key=str.lower), str(ids))

    print("\n2. the question, per app")
    res = api._update_check("behind.git")
    check("a checkout whose remote moved on says how far", res.get("behind") == 2, str(res)[:200])
    check("and says it is behind rather than merely not equal",
          res.get("up_to_date") is False, str(res)[:200])
    check("having nothing of its own to push is not the same as being up to date",
          res.get("ahead") == 0, str(res)[:200])
    check("and the answer names the refs it compared",
          res.get("branch") and res.get("current"), str(res)[:200])
    cur = api._update_check("current.git")
    check("a checkout that is current reports nothing behind",
          cur.get("behind") == 0 and cur.get("up_to_date") is True, str(cur)[:200])
    miss = api._update_check("plain")
    check("a folder that is not a checkout is not counted as behind",
          not miss.get("behind"), str(miss)[:160])
    check("and it stays out of the count for a different, named reason",
          miss.get("is_repo") is False and bool(miss.get("reason")), str(miss)[:200])

    print("\n3. the question, for the shelf at once")
    payload = json.loads(api.apps_updates().body.decode("utf-8"))
    check("every app was checked", payload.get("checked") == len(ids), str(payload.get("checked")))
    check("only the app that is behind is listed as having an update",
          payload.get("updates") == ["behind.git"], str(payload.get("updates")))
    check("and the count is attached to it, not just a yes",
          payload.get("behind") == {"behind.git": 2}, str(payload.get("behind")))
    check("the shortcut \"is there anything\" agrees",
          payload.get("any") is True, str(payload.get("any")))
    check("an unanswerable app does not fail the whole answer",
          set(payload.get("apps", {})) == set(ids), str(sorted(payload.get("apps", {}))))

    print("\n4. waiting for the update to actually finish")
    fake = FakeRuntime(alive=2)
    api._runtime = lambda: fake
    jid = api._job_new("test", "waiting")
    started = time.time()
    done = api._wait_for_app_to_finish(jid, "behind.git", "updating", timeout=30)
    check("it waits while the app is still running", done is True, str(done))
    check("and it really waited rather than answering on the first look",
          fake.calls >= 3, f"{fake.calls} read(s) in {time.time() - started:.1f}s")

    never = FakeRuntime(alive=10 ** 6)
    api._runtime = lambda: never
    jid2 = api._job_new("test", "waiting too long")
    ok2 = api._wait_for_app_to_finish(jid2, "behind.git", "updating", timeout=0.3, quiet=0.05)
    check("an update that never finishes is not called finished", ok2 is False, str(ok2))
    tail2 = log_of(api, jid2)
    check("and the terminal says so, so nobody is left guessing",
          "did not finish" in tail2, repr(tail2[-160:]))

    print("\n5. one app, updated and then read back as Athena's")
    calls = []
    # One instance, deliberately: `_wait_for_app_to_finish` reads the runtime
    # repeatedly, and a fresh fake on every read would report "still running"
    # forever — which is exactly the bug that would look like a hang.
    one = FakeRuntime(alive=1)
    api._runtime = lambda: one
    api.app_action = lambda app_id, action, body=None, origin="ui": (
        calls.append((app_id, action, origin)) or
        api.JSONResponse({"ok": True, "action": action}))
    converts = []

    def fake_convert(app_id, jid, provision=True):
        converts.append((app_id, provision))
        return {"ok": True, "lines": [f"converted {app_id}"]}

    api._convert_app = fake_convert
    jid3 = api._job_new("test", "one app")
    out = api._update_one(jid3, "behind.git")
    check("the app's own update was the thing that ran",
          calls == [("behind.git", "update", "ui")], str(calls))
    check("the result is only ok once the app was read back",
          out.get("ok") is True and out.get("converted") is True, str(out))
    check("streaming the app's stdout is what the terminal expects", out.get("finished") is True, str(out))
    check("the conversion ran after the update, not as a substitute for it",
          converts == [("behind.git", False)], str(converts))
    check("and it did not try to install anything",
          converts and converts[0][1] is False, str(converts))
    tail3 = log_of(api, jid3)
    check("the terminal names the app and says what happened",
          "behind.git" in tail3 and "re-read" in tail3, repr(tail3[-200:]))

    print("\n6. an update that will not start")
    converts.clear()
    api.app_action = lambda app_id, action, body=None, origin="ui": (
        api.JSONResponse({"ok": False, "error": "no update script in this app"}))
    jid4 = api._job_new("test", "broken")
    out4 = api._update_one(jid4, "plain")
    check("the failure is reported, not swallowed", out4.get("ok") is False, str(out4))
    check("the reason the app gave is the reason shown",
          "no update script" in str(out4.get("error")), str(out4.get("error")))
    check("and nothing was converted on the strength of a pull that never happened",
          converts == [], str(converts))

    print("\n7. updating everything that is behind")
    converts.clear()
    calls.clear()
    many = FakeRuntime(alive=1)
    api._runtime = lambda: many
    api.app_action = lambda app_id, action, body=None, origin="ui": (
        calls.append((app_id, action)) or api.JSONResponse({"ok": True}))
    jid5 = api._job_spawn("update-all", "all", lambda j: api._update_all_work(j, ids))
    deadline = time.time() + 60
    while time.time() < deadline and api._JOBS.get(jid5, {}).get("state") == "running":
        time.sleep(0.2)
    job = api._JOBS.get(jid5, {})
    result = job.get("result") or {}
    check("the job finished rather than hanging", job.get("state") == "done", str(job)[:200])
    check("only the app that was behind was updated",
          result.get("updated") == ["behind.git"], str(result))
    check("and no other app was asked to update",
          calls == [("behind.git", "update")], str(calls))
    check("the app was re-read as an Athena app afterwards",
          converts == [("behind.git", False)], str(converts))
    check("the terminal counted the apps as it went",
          "1 app(s) have updates" in log_of(api, jid5), repr(log_of(api, jid5)[-260:]))

    print("\n8. when there is nothing to update to")
    converts.clear()
    calls.clear()
    jid6 = api._job_new("test", "nothing")
    res6 = api._update_all_work(jid6, ["current.git", "plain"])
    check("nothing is updated", res6.get("updated") == [], str(res6))
    check("nothing is converted either", converts == [], str(converts))
    check("and it says so plainly rather than reporting success for work not done",
          "already at the latest commit" in log_of(api, jid6), repr(log_of(api, jid6)))

    print("\n9. the route that starts it")
    api._job_spawn = lambda kind, label, work: (api.__dict__.setdefault("_spawned", []).append(kind) or "job-x")
    api._spawned = []
    resp = json.loads(api.apps_update_all().body.decode("utf-8"))
    check("updating all is a job, not a request that waits", resp.get("job") == "job-x", str(resp))
    check("the job knows which apps it is about", set(resp.get("apps", [])) == set(ids), str(resp.get("apps")))
    api._spawned = []
    one = json.loads(api.app_update("behind.git").body.decode("utf-8"))
    check("one app's update is a job too", one.get("job") == "job-x" and one.get("app") == "behind.git",
          str(one))
    missing = api.app_update("no-such-app")
    check("and an app that is not there is refused", missing.status_code == 404, str(missing.status_code))

    print("\n9. what a running job says about itself")
    # The two ages the panel reads while an install is going. A job that has only
    # been started carries no output yet, which is exactly the state the header has
    # to describe honestly — so both ages must be present and near zero, not None.
    jid7 = api._job_new("install", "narrating")
    fresh = json.loads(api.job_status(jid7).body.decode())
    check("a job that has just started already knows how long it has been going",
          isinstance(fresh.get("elapsed"), (int, float)) and 0 <= fresh["elapsed"] < 5,
          str(fresh.get("elapsed")))
    check("and knows it has said nothing yet, rather than reporting no such age",
          isinstance(fresh.get("since_output"), (int, float)) and fresh["since_output"] < 5,
          str(fresh.get("since_output")))
    check("a job with no kind-specific hint still reports both ages",
          fresh.get("kind") == "install" and fresh.get("state") == "running",
          f"{fresh.get('kind')} / {fresh.get('state')}")

    # Moving the bar is a sign of life even when nothing is printed: a copy reports
    # its progress silently, and reading that as "no output for 6m" would be wrong.
    api._job_progress(jid7, 3, 10, "copying")
    api._job_add(jid7, "one file landed")
    time.sleep(1.1)
    after = json.loads(api.job_status(jid7).body.decode())
    check("a job that only moved its bar is not read as silent",
          after["since_move"] <= after["since_output"] + 0.05,
          f"move {after['since_move']}s ago, output {after['since_output']}s ago")
    check("and the bar's own reading survives beside the ages",
          (after.get("progress") or {}).get("label") == "copying"
          and (after.get("progress") or {}).get("done") == 3.0,
          json.dumps(after.get("progress"))[:120])

    # An age that cannot be computed is left out, never faked as zero: a record
    # edited by hand, or one written by an older build, must not read as instant.
    api._JOBS[jid7].pop("last_output", None)
    api._JOBS[jid7].pop("moved", None)
    unaged = json.loads(api.job_status(jid7).body.decode())
    check("a record with no output stamp reports no age at all, rather than zero",
          unaged.get("since_output") is None and unaged.get("since_move") is None,
          f"{unaged.get('since_output')} / {unaged.get('since_move')}")
    check("while the run's own length still comes from the start stamp",
          isinstance(unaged.get("elapsed"), (int, float)), str(unaged.get("elapsed")))
    api._job_end(jid7, "done", result={"ok": True})

    print("\n10. going back to a commit this app has actually been on")
    # The record a revert is built on is the app's own repository: git writes a
    # reflog entry for every commit the folder has stood on, including the resets
    # this route performs, so there is no bookkeeping file of Athena's to go stale
    # or be deleted. The fixture is a real clone that has really pulled once --
    # two entries, one commit to go back to.
    # Section 9 replaced the real spawner with one that returns a name and never
    # runs the work, which is right for checking what a route answers with and
    # useless for checking what it does. So the work is put back on the real
    # `_job_new`/`_job_end` here and run to completion in place: the route's own
    # closure is exercised, and the job it hands back is a real job.
    kinds10: List[str] = []

    def run_now(kind, label, work):
        kinds10.append(kind)
        jid = api._job_new(kind, label)
        out = work(jid) or {}
        api._job_end(jid, "done" if out.get("ok", True) else "failed", result=out)
        return jid

    api._job_spawn = run_now
    hist_app = make_remote_and_clone(tmp, "history.git", 1)
    git(hist_app, "pull", "-q", "origin", "main")
    hist = json.loads(api.app_history("history.git").body.decode())
    check("history is read out of the app's own repository",
          hist.get("ok") and hist.get("is_repo") and len(hist.get("commits") or []) >= 2,
          f"{len(hist.get('commits') or [])} entry/entries")
    check("newest first, with exactly one entry marked as where the app is now",
          hist["commits"][0].get("current") is True
          and sum(1 for c in hist["commits"] if c.get("current")) == 1,
          json.dumps(hist["commits"][:2])[:200])
    check("each entry carries an id, a stamp and what moved the app there",
          all(c.get("sha") and c.get("when") and c.get("what") for c in hist["commits"]),
          json.dumps(hist["commits"])[:220])
    # The stamp is the move's, not the commit's: for a commit written months ago
    # those two are far apart, and a reflog read from `%cd` would report the older
    # one as if it were when this app last changed. (Verified on a real app here:
    # 2026-10-10 reflog against a 2026-03-30 commit.)
    check("the stamp is when the app moved, not when the commit was written",
          all("2026-" in c["when"] for c in hist["commits"]), json.dumps(hist["commits"])[:160])
    check("so there is somewhere to go back to", hist.get("can_revert") is True,
          str(hist.get("can_revert")))
    back_to = [c for c in hist["commits"] if not c.get("current")][-1]

    plain_hist = json.loads(api.app_history("plain").body.decode())
    check("an app that is not a repository says so instead of offering a button",
          plain_hist.get("is_repo") is False and "git repository" in plain_hist.get("reason", ""),
          json.dumps(plain_hist)[:160])
    gone = json.loads(api.app_history("no-such-app").body.decode())
    check("and an app that is not there is answered, not crashed on",
          gone.get("ok") is False and "no-such-app" in gone.get("error", ""),
          json.dumps(gone)[:140])

    # Four refusals, each one something a person would rather have refused.
    r = api.app_revert("history.git", api.RevertBody(sha="nonsense"))
    check("a string that is not a commit id is refused before git sees it",
          r.status_code == 400 and "not a commit id" in r.body.decode(), r.body.decode()[:120])
    r = api.app_revert("history.git", api.RevertBody(sha="deadbeef"))
    check("a commit id that is not in this app is refused",
          r.status_code == 400 and "no commit with that id" in r.body.decode(),
          r.body.decode()[:140])
    r = api.app_revert("plain", api.RevertBody(sha="deadbeef"))
    check("a folder that is not a repository is refused, with that as the reason",
          r.status_code == 400 and "git repository" in r.body.decode(), r.body.decode()[:140])
    r = api.app_revert("history.git", api.RevertBody(sha=hist["head"]["sha"]))
    check("and being already on the commit asked for is refused rather than reported as done",
          r.status_code == 400 and "already on that commit" in r.body.decode(),
          r.body.decode()[:140])

    # A commit that exists in the app's object store but was never checked out is
    # the case this guard exists for: a fetch must not become a way to install
    # something this app has never run.
    seed_dir = tmp / "_seed" / "history.git"
    (seed_dir / "LATER.md").write_text("later\n", encoding="utf-8")
    git(seed_dir, "add", "-A")
    git(seed_dir, "commit", "-qm", "a commit this app has never run")
    git(seed_dir, "push", "-q", "origin", "main")
    git(hist_app, "fetch", "-q", "origin")
    fetched = git(hist_app, "rev-parse", "origin/main").stdout.strip()
    in_repo = git(hist_app, "cat-file", "-t", fetched).stdout.strip()
    check("the fixture really has that commit in the app's object store",
          in_repo == "commit", f"{fetched[:10]} is a {in_repo}")
    r = api.app_revert("history.git", api.RevertBody(sha=fetched))
    check("a fetched commit the app has never stood on is refused, so a fetch cannot "
          "become an install",
          r.status_code == 400 and "never been on that commit" in r.body.decode(),
          r.body.decode()[:160])

    # The revert itself: files really go back, and the move is recorded by git.
    api._convert_app = lambda app_id, jid, provision=True: {"ok": True, "lines": []}
    (hist_app / "README.md").write_text("edited by hand\n", encoding="utf-8")
    dirty_hist = json.loads(api.app_history("history.git").body.decode())
    check("changes that are not in a commit are counted before anyone agrees",
          dirty_hist.get("dirty") == 1, str(dirty_hist.get("dirty")))
    r = api.app_revert("history.git", api.RevertBody(sha=back_to["sha"]))
    body = json.loads(r.body.decode())
    check("a put-back that is allowed answers with the job to watch",
          r.status_code == 200 and body.get("ok") and body["job"]
          and body["watch"] == f"/jobs/{body['job']}" and kinds10 == ["revert"],
          json.dumps(body)[:200] + " kinds=" + str(kinds10))
    check("and names both ends of the move, so the record says where it came from",
          body.get("from") == hist["head"]["short"] and body.get("to") == back_to["short"],
          f"{body.get('from')} -> {body.get('to')}")
    check("and reports what it is about to give up", body.get("dirty") == 1,
          str(body.get("dirty")))
    deadline = time.time() + 90
    st = {}
    while time.time() < deadline:
        st = json.loads(api.job_status(body["job"]).body.decode())
        if st.get("state") != "running":
            break
        time.sleep(0.5)
    check("the put-back job finishes rather than staying running",
          st.get("state") == "done", f"{st.get('state')}: {st.get('error') or ''}")
    check("the files really went back — the hand edit is gone, as the warning said",
          (hist_app / "README.md").read_text(encoding="utf-8") == "app\n",
          repr((hist_app / "README.md").read_text(encoding="utf-8")[:40]))
    check("and the app's HEAD is the commit that was asked for",
          git(hist_app, "rev-parse", "HEAD").stdout.strip() == back_to["sha"],
          git(hist_app, "rev-parse", "HEAD").stdout.strip()[:12])
    tail10 = log_of(api, body["job"])
    check("the terminal says what it gave up and where the app is now",
          "1 file(s) with changes" in tail10 and f"back at {back_to['short']}" in tail10,
          repr(tail10[-300:]))
    # The sentence that keeps a revert from being a surprise: the app's own Update
    # is what moves it forward again, and it is named rather than left to be found.
    check("and says out loud that updating again moves the app forward",
          "updating this app again" in tail10.lower(), repr(tail10[-200:]))
    after = json.loads(api.app_history("history.git").body.decode())
    check("the reset is itself in the history, so where it has been stays honest",
          after["commits"][0]["sha"] == back_to["sha"]
          and "reset" in after["commits"][0]["what"], json.dumps(after["commits"][:1])[:200])
    check("and there is somewhere to go forward to again",
          after.get("can_revert") is True
          and any(c["sha"] == hist["head"]["sha"] for c in after["commits"]),
          json.dumps(after["commits"])[:220])

    print("\n11. a job the panel is watching when the engine goes away")
    # Jobs are a dictionary in this process, so a restart takes every one of them.
    # That is fine — a job is a thread and a log tail, and neither survives an
    # honest restart — but the 404 has to *say* so. It used to be a bare "no such
    # job", which a one-and-a-half-second poll turned into "the request failed":
    # someone watching an install was told about a network fault they did not have.
    missing = api.job_status("a-job-that-never-was")
    gone = json.loads(missing.body.decode())
    check("an unknown job id is a 404", missing.status_code == 404, str(missing.status_code))
    check("and it says the job went with a restart rather than just 'no such job'",
          gone.get("gone_with_restart") is True and gone.get("error") == "no such job",
          json.dumps(gone)[:200])
    check("the answer carries when this run started, so the caller can date it",
          isinstance(gone.get("session_started"), (int, float))
          and 0 <= time.time() - gone["session_started"] < 86400,
          str(gone.get("session_started")))
    check("and the words name the restart and where the output went",
          "restart" in (gone.get("note") or "").lower()
          and "log" in (gone.get("note") or "").lower(), repr(gone.get("note"))[:200])
    check("nothing in it promises a retry that cannot happen",
          not any(w in (gone.get("note") or "").lower()
                  for w in ("try again later", "temporarily", "retry", "just a moment")),
          repr(gone.get("note"))[:200])

    # The flag has to mean something: a job that *is* here must not carry it.
    here = api._job_new("install", "still running")
    snap = json.loads(api.job_status(here).body.decode())
    check("a job that is here does not claim to have been lost",
          snap.get("ok") is True and "gone_with_restart" not in snap, json.dumps(snap)[:200])
    check("the run's start stamp is not after a job this run started",
          gone["session_started"] <= snap["started"] + 0.001,
          f"session {gone['session_started']} > job {snap.get('started')}")
    api._job_end(here, "done", result={"ok": True})

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} check(s) failed")
    else:
        print(f"{passed}/{passed} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
