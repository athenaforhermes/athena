"""The engine supervisor: one pinokiod child process, and the truth about it.

Split out of `plugin_api.py` (rule 136). 561 lines that own a subprocess, a port, a log
and the readiness question -- the piece of Athena that most needs to be readable on its
own, and the piece that least needs to know about HTTP routes.

What it takes from its caller (the API module) rather than importing:

* `engine_dir`, `plugin_dir` -- where the engine and the browser shims live.
* `load_settings`, `save_settings` as callables -- because the settings *paths* stay in the
  API module, where three harnesses patch them (rule 153). A manager that imported the store
  directly would read and write a different file than the one the tests pointed at.

What it owns outright: the process, the port override, the read log, the readiness memo,
the `pids_from_*` parsers, and the 60-second listen memo that used to be defined four
thousand lines below its only reader.

Imported by name, because `plugin_api.py` is itself loaded by path (rule 136).
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import node_runtime as athena_node

# The readiness memo. Process-wide, deliberately: it caches the answer to "is anything listening
# on our port", and a second manager asking the same question should get the memo, not a shadow of
# it. These two names used to sit four thousand lines below their only reader in `plugin_api.py`.
_PROBE_LOCK = threading.Lock()
_PROBE_AT: List[Any] = [0.0, False]


# ---------------------------------------------------------------- engine manager


def _pids_from_lsof(out: str) -> Set[str]:
    """`lsof -ti` prints one bare pid per line and nothing else."""
    return {line.strip() for line in out.splitlines() if line.strip().isdigit()}


def _pids_from_ss(out: str) -> Set[str]:
    """`ss -ltnpH` prints one line per socket, with `pid=1234` among the fields."""
    return set(re.findall(r"pid=(\d+)", out))


# How long a probe may wait before a port is called asleep. A cold engine accepts
# connections for a while before it answers HTTP, and every /status during that
# window used to block for the full engine timeout — measured 2.0s per call on a
# 5s UI clock while the whole boot was only a couple of seconds, which is what
# "Athena takes 10-30 seconds to start" was made of. 0.4s is long enough to tell
# an engine that is up from one that is still coming up, and short enough that a
# poll never makes the panel wait. Module level because it is a default argument
# of `EngineManager.listening`, evaluated when the class is built.
_STARTING_PROBE = 0.4

class EngineManager:
    """Owns the pinokiod child process: start, stop, readiness, proxy base."""

    def __init__(self, engine_dir: Path, plugin_dir: Path, load_settings, save_settings) -> None:
        # Injected, not imported: the settings paths are the API module's globals and three
        # harnesses redirect a run by patching them, so the manager asks its caller every time
        # rather than capturing a path of its own (rule 153).
        self.engine_dir = engine_dir
        self.plugin_dir = plugin_dir
        self._load_settings = load_settings
        self._save_settings = save_settings
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._log: List[str] = []
        self._started_at: Optional[float] = None
        self._booting_since: Optional[float] = None
        # When this process first saw the engine coming up. Ours is measured from its
        # spawn time; an engine somebody else started is measured from this, because it
        # is the earliest moment this process can honestly claim to know about.
        self._last_error: str = ""
        # Set when the configured port is held by something that is not the
        # engine: this process then uses the fallback for the rest of its life.
        self._port_override: Optional[int] = None
        self._owner_at: float = 0.0
        self._owner_pid: Optional[int] = None
        # [when, is anything listening]. Only a NO is remembered, and only for a
        # couple of seconds: while the engine is down the panel polls every
        # second, and a netstat plus a connect that has to time out is most of a
        # second of a call whose whole answer is "still nothing".
        self._listen_at: List[Any] = [0.0, True]

    # -- helpers ---------------------------------------------------------

    def settings(self) -> Dict[str, Any]:
        return self._load_settings()

    @property
    def home(self) -> Path:
        return Path(self.settings()["home"])

    @property
    def port(self) -> int:
        return int(self._port_override or self.settings()["port"])

    @property
    def profile_dir(self) -> Path:
        return self.home / ".engine-profile"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _note(self, line: str) -> None:
        self._log.append(line)
        if len(self._log) > 400:
            del self._log[:200]

    # -- state -----------------------------------------------------------

    def is_running(self) -> bool:
        """True only for an engine THIS process spawned — see `port_owner()`."""
        return self._proc is not None and self._proc.poll() is None

    def listening(self, timeout: float = _STARTING_PROBE) -> bool:
        """Is anything at all holding our port? Decided in a fraction of a second.

        A TCP connect is the honest test, and this host is why it needs a budget:
        against a port with nothing behind it, Windows here DROPS the SYN instead
        of refusing it — measured 2.0s to fail against 127.0.0.1 and 4.0s against
        a name that resolves to ::1 first — so a probe of a dead port costs its
        whole timeout instead of failing instantly. A connect that has not landed
        within the budget is not an engine answering HTTP, so it is reported the
        same as nothing there; both callers handle that.
        """
        now = time.monotonic()
        if not self._listen_at[1] and now - self._listen_at[0] < 2.0:
            return False
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout)
        except OSError:
            s = None
        if s is None:
            self._listen_at = [now, False]
            return False
        s.close()
        self._listen_at = [now, True]
        return True

    def probe(self, timeout: float = 1.5, ttl: float = 1.0) -> bool:
        """True when the engine answers HTTP on its port.

        Cached for `ttl` seconds, single-flighted: every caller that needs to
        know whether the engine is up used to pay its own blocking round trip,
        so a busy engine stalled a whole set of concurrent polls. Pass ttl=0
        where a stale answer would change a decision — adopting a port, deciding
        to spawn — rather than just what a panel shows.
        """
        now = time.monotonic()
        with _PROBE_LOCK:
            if ttl and now - _PROBE_AT[0] < ttl:
                return _PROBE_AT[1]
        # A port with nothing behind it should be answered by a connect, not by an
        # HTTP round trip. On a host that drops the SYN of a dead port instead of
        # refusing it, `urlopen` spends the caller's whole timeout failing —
        # measured 1.5s on `/apps` and 2.0s inside every app health check while the
        # engine was stopped. `listening` remembers a NO for two seconds, which is
        # what makes a repeated poll free.
        if not self.listening(timeout=min(timeout, _STARTING_PROBE)):
            with _PROBE_LOCK:
                _PROBE_AT[0] = time.monotonic()
                _PROBE_AT[1] = False
            return False
        try:
            req = Request(self.base_url + "/", method="GET")
            with urlopen(req, timeout=timeout) as r:
                ok = 200 <= r.status < 500
        except HTTPError as e:
            ok = 200 <= e.code < 500
        except (URLError, OSError):
            ok = False
        with _PROBE_LOCK:
            _PROBE_AT[0] = time.monotonic()
            _PROBE_AT[1] = ok
        return ok

    def _port_pids(self) -> Set[str]:
        """PIDs listening on our port, whoever spawned them."""
        if sys.platform != "win32":
            # lsof is the portable answer where it exists and `ss` covers the hosts
            # that ship it instead. Neither being present is survivable: the engine
            # itself still stops by pid, and this only sweeps what a crash left
            # holding the port.
            for cmd, parse in ((["lsof", "-ti", f"tcp:{self.port}", "-sTCP:LISTEN"],
                                _pids_from_lsof),
                               (["ss", "-ltnpH", f"sport = :{self.port}"],
                                _pids_from_ss)):
                try:
                    out = subprocess.run(cmd, capture_output=True, text=True,
                                         timeout=15).stdout
                except Exception:  # noqa: BLE001
                    continue
                pids = parse(out)
                if pids:
                    return {p for p in pids if p != str(os.getpid())}
            return set()
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=15,
            ).stdout
        except Exception:  # noqa: BLE001
            return set()
        pids: Set[str] = set()
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{self.port}"):
                pids.add(parts[-1])
        return {pid for pid in pids
                if pid.isdigit() and pid != str(os.getpid())}

    def _identify(self, timeout: float = 2.0) -> str:
        """What is answering on our port: `ours`, `foreign` or `none`.

        `probe()` only proves that SOMETHING speaks HTTP there, and adopting on
        that evidence alone would let Athena claim — and Stop would then kill — a
        service that merely happens to hold the port. An engine is the only
        thing that answers `/info/scripts` with a JSON object, so that is the
        test.
        """
        if not self.probe(timeout, ttl=0):
            return "none"
        try:
            req = Request(self.base_url + "/info/scripts", method="GET")
            with urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except (HTTPError, URLError, OSError, ValueError):
            return "foreign"
        try:
            return "ours" if isinstance(json.loads(raw), dict) else "foreign"
        except (ValueError, TypeError):
            return "foreign"

    def port_owner(self, ttl: float = 15.0) -> Optional[int]:
        """The PID serving our port, even one this process has no handle on.

        `_proc` only exists for an engine this process spawned. A backend
        restart — a Hermes update, or the app being quit and reopened — leaves
        the engine running with its parent gone and the port still held, while
        `_proc` is None and `is_running()` therefore reports stopped. Starting
        again then spawned a SECOND engine that died on the spot with
        `listen EADDRINUSE: address already in use :::42001` while the orphan
        carried on serving, which is what "Athena starts and then stops itself
        after ~10s" actually was. Ask this before ever spawning one.
        """
        # `_port_pids` shells out to netstat, so the answer is memoised briefly:
        # the owner of a port does not change often, and this is reachable from
        # `status()`, which the UI polls.
        now = time.time()
        if now - self._owner_at < ttl:
            return self._owner_pid
        pids = self._port_pids()
        pid = int(min(pids, key=lambda q: int(q))) if pids else None
        self._owner_at, self._owner_pid = now, pid
        return pid

    def status(self) -> Dict[str, Any]:
        running = self.is_running()
        if running:
            # Cheap path. A handle means the process is ours, so one probe
            # answers "ready yet" and nothing needs to shell out.
            # Short probe: with a handle the process is ours either way, so the
            # only question is whether it has started answering yet — and a poll
            # that waits the full engine timeout for that answer is 1.5s of a
            # panel that is trying to draw a boot.
            serving = self.probe(_STARTING_PROBE)
            state, adopted = ("ours" if serving else "starting"), False
        else:
            # No handle: something may still hold our port — an engine left by a
            # previous process, which is the normal case after any restart, and
            # the state every poll is in while one is booting. Ask who owns it
            # first (memoised: one netstat per 15s, not one per poll).
            if self.port_owner() is not None:
                # A held port that accepts connections but says nothing is an
                # engine still coming up, so a short probe settles it. Waiting
                # the full engine timeout here — on every poll — is what made a
                # 6s boot feel like half a minute. When it does answer, the same
                # short timeout is enough to tell ours from foreign.
                state = ("starting" if not self.probe(_STARTING_PROBE, ttl=0)
                         else self._identify(timeout=_STARTING_PROBE))
            elif self.listening():
                # Something took the port between the netstat and now, or the
                # netstat could not run. A held port that accepts but says nothing
                # is an engine coming up.
                state = ("starting" if not self.probe(_STARTING_PROBE, ttl=0)
                         else self._identify(timeout=_STARTING_PROBE))
            else:
                # Nothing is listening. On a host that drops the SYN of a dead
                # port instead of refusing it (see `listening`), asking the HTTP
                # layer anything here costs the whole timeout — 2.0s of every poll
                # while the engine is down. Netstat said nothing holds the port and
                # a connect landed nowhere: the engine is not there, and no probe
                # can say more than that.
                state = "none"
            serving = state == "ours"
            adopted = serving
        # How long the wait has been. "Athena takes a while to start" is a report nobody
        # can act on — the person waiting needs a number, and the panel is the only thing
        # in a position to count it. Measured from the spawn when the engine is ours, and
        # from the first poll that caught it coming up when it is not.
        if state == "starting":
            if self._booting_since is None:
                self._booting_since = time.time()
        else:
            self._booting_since = None
        since = self._started_at or self._booting_since
        return {
            # an engine we did not spawn is still a live engine: saying
            # "stopped" over a serving port is what made Start look like the
            # only way forward, and Start was what kept failing
            "running": running or adopted,
            "serving": serving,
            "ready": serving,
            "adopted": adopted,
            # `foreign` means another program holds the port — the UI can say so
            # instead of offering a Start that cannot succeed
            "engine_state": state,
            # A held-but-silent port is a boot in progress, and the panel says
            # "starting…" for it rather than "stopped". Its own field so nothing
            # has to infer it from three others.
            "starting": state == "starting",
            # Milliseconds spent waiting so far, or None when there is no honest start
            # to measure from (an engine that is simply up, or one nothing is waiting on).
            "starting_ms": (int((time.time() - since) * 1000)
                            if state == "starting" and since else None),
            "pid": (self._proc.pid if running and self._proc
                    else (self.port_owner() if state in ("ours", "starting") else None)),
            "port": self.port,
            "url": self.base_url,
            "home": str(self.home),
            "uptime": (time.time() - self._started_at) if (running and self._started_at) else None,
            "node": athena_node.find_node(),
            "engine_dir": str(self.engine_dir),
            "engine_present": (self.engine_dir / "node_modules" / "pinokiod" / "package.json").exists(),
            "last_error": self._last_error,
            "log": self._log[-40:],
        }

    # -- lifecycle -------------------------------------------------------

    def _identify_if_taken(self) -> str:
        """`_identify()` — but only when something is plausibly holding the port.

        Identifying a port with nothing behind it is not free on every host: this
        one drops the SYN instead of refusing it, so `_identify` waits out its
        timeout (measured 2.0s against 127.0.0.1, 4.0s where the name resolves to
        ::1 first) on every Start click and, before this, on every poll. Netstat
        and a short connect answer "is anything there at all" for a fraction of
        that; only if something is does the patient identification run.
        """
        if self.port_owner() is None and not self.listening():
            return "none"
        return self._identify()

    def start(self, wait: float = 60.0) -> Dict[str, Any]:
        with self._lock:
            if self.is_running():
                if self.probe():
                    return {"ok": True, "already": True, **self.status()}
                # process alive but not serving yet — let the caller wait
            elif (state := self._identify_if_taken()) == "ours":
                # An engine we did not spawn is already serving on our port.
                # Adopt it: spawning a second copy can only end in EADDRINUSE,
                # and the adopted one is already doing the job.
                self._owner_at = 0.0
                pid = self.port_owner()
                self._note(f"adopted running engine pid={pid} port={self.port}")
                return {"ok": True, "adopted": True, **self.status()}
            else:
                # A `foreign` port means something that is not an engine already
                # owns it, so a copy spawned here could only die with EADDRINUSE.
                # Pick a free port, then FALL THROUGH into the one spawn path
                # below. This must not be its own branch: the spawn lives inside
                # this `else`, so a separate arm skipped it entirely and start()
                # reported "engine exited early" for an engine it never launched.
                if state == "foreign":
                    free = athena_node.find_free_port(self.port + 1)
                    if free is None:
                        msg = (f"port {self.port} is held by another program and no "
                               f"free port near it was found")
                        self._last_error = msg
                        self._note(msg)
                        return {"ok": False, "error": msg, **self.status()}
                    self._note(f"port {self.port} is held by another program — "
                               f"using {free} instead")
                    cfg = self._load_settings()
                    cfg["port"] = free
                    try:
                        self._save_settings(cfg)
                    except Exception:  # noqa: BLE001
                        pass
                    # Authoritative for this process either way: ATHENA_PORT in
                    # the environment would otherwise override what was just
                    # saved and send the next start back into the conflict.
                    self._port_override = free
                node = athena_node.find_node()
                if not node:
                    self._last_error = (
                        "No compatible Node runtime found. Athena needs Node 22.x "
                        "(pinokiod's native addons have no prebuilds for Node 25/26)."
                    )
                    return {"ok": False, "error": self._last_error}
                if not (self.engine_dir / "node_modules" / "pinokiod").exists():
                    self._last_error = f"Engine not installed at {self.engine_dir}"
                    return {"ok": False, "error": self._last_error}

                home = self.home
                profile = self.profile_dir
                home.mkdir(parents=True, exist_ok=True)
                profile.mkdir(parents=True, exist_ok=True)
                for sub in ("api", "bin", "cache", "logs"):
                    (home / sub).mkdir(parents=True, exist_ok=True)

                env = dict(os.environ)
                # Isolation: os.homedir() honors USERPROFILE on Windows, so the
                # kernel store lands under our profile dir, not the real ~/.pinokio.
                env["USERPROFILE"] = str(profile)
                env["HOME"] = str(profile)
                env["ATHENA_HOME"] = str(home)
                env["ATHENA_PORT"] = str(self.port)
                # Athena's own names go in; the engine's legacy ones are dropped so a
                # stale value from the user's shell cannot steer this child. (The
                # engine re-exports its own names into every app it runs — see
                # docs/NAMING.md for the mapping.)
                for _legacy in ("PINOKIO_HOME", "PINOKIO_SCRIPT_AUTOLAUNCH",
                                "PINOKIO_SCRIPT_REQUIRES"):
                    env.pop(_legacy, None)
                # An app must not open a web browser of its own. Gradio's
                # `inbrowser=True` — and any launcher that shells out to `start`
                # — puts the app's page in the user's default browser: outside
                # the window Athena draws for it, and outside the idle reaper's
                # sight (an app being typed into in that browser was reaped as
                # idle after 120s). Python's webbrowser runs $BROWSER in place of
                # a real browser, so this makes the call a no-op. Verified by
                # process count: the control run spawned a browser, this did not.
                for _name in (("no-browser.cmd",) if sys.platform == "win32"
                              else ("no-browser.sh", "no-browser.cmd")):
                    _no_browser = self.plugin_dir / "tools" / _name
                    if _no_browser.is_file():
                        env["BROWSER"] = str(_no_browser)
                        break
                # CUDA/torch tuning, applied to EVERY app at once. The engine's
                # env is the base of each app's env (the kernel builds the child
                # env from process.env), so one edit here covers all apps — where
                # Pinokio would need a per-app environment.json edit.
                #
                # expandable_segments is the one that matters: the default caching
                # allocator fragments over a long session and then stalls retrying
                # allocations, which reads as "generation randomly got slower".
                # Growable segments avoid that. LAZY module loading trims cold
                # start; it is the CUDA 12.2+ default, set explicitly so a future
                # downgrade cannot silently reintroduce eager loading.
                #
                # setdefault, not assignment: if you already export either of
                # these, or an app sets them in its own environment.json (which
                # is applied later and wins), your value is left alone.
                #
                # Deliberately NOT set: TORCH_ALLOW_TF32_CUBLAS_OVERRIDE and
                # NVIDIA_TF32_OVERRIDE. They only affect fp32 matmuls, and this
                # stack runs bf16/int8 with flash-attn — so they would change
                # numerics for no measurable speed. cudnn.benchmark has no env
                # var at all (it is a torch API call), so it cannot be set here.
                env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
                env.setdefault("CUDA_MODULE_LOADING", "LAZY")
                # Credentials, as environment rather than as files an app has to go looking
                # for. The engine re-exports its env into every app it runs, so one edit here
                # reaches all of them — and an environment variable leaves no plaintext copy
                # behind, which is the whole point. The file forms under cache/HF_AUTH and
                # connect/ remain only for the apps whose own code reads a path that no
                # environment can reach.
                #
                # `setdefault`: an export you already have wins, and an app's own
                # environment.json — applied later, per app — wins over that.
                #
                # Measured cost where this was written: about 0.7s, once per engine start,
                # nearly all of it the `gh auth token` call. Bounded and non-fatal by
                # construction — on a machine whose credential sources are unreachable the
                # engine launches exactly as before, it simply receives none from here.
                try:
                    import credentials as _athena_credentials
                    for _ck, _cv in _athena_credentials.credential_env(self).items():
                        env.setdefault(_ck, _cv)
                except Exception:  # noqa: BLE001
                    pass

                creation = 0
                if sys.platform == "win32":
                    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    creation |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

                try:
                    self._proc = subprocess.Popen(
                        [node, str(self.engine_dir / "boot.js")],
                        cwd=str(self.engine_dir),
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        stdin=subprocess.DEVNULL,
                        creationflags=creation,
                        # POSIX has no process group flag; a session is what makes
                        # the reaper above able to kill the tree rather than the top
                        # of it.
                        start_new_session=(sys.platform != "win32"),
                    )
                except Exception as e:  # noqa: BLE001
                    self._last_error = f"Failed to spawn engine: {e}"
                    return {"ok": False, "error": self._last_error}

                self._started_at = time.time()
                self._last_error = ""
                self._note(f"spawned engine pid={self._proc.pid} port={self.port} home={home}")

            # wait for HTTP readiness
            deadline = time.time() + wait
            while time.time() < deadline:
                if not self.is_running():
                    self._last_error = f"Engine exited early (code {self._proc.returncode if self._proc else '?'})"
                    return {"ok": False, "error": self._last_error}
                # A short probe with a short nap between: readiness is seen
                # within ~0.6s instead of ~2s, and the caller's wait is spent on
                # the engine's own boot rather than on our polling granularity.
                if self.probe(timeout=_STARTING_PROBE):
                    self._note("engine ready")
                    return {"ok": True, **self.status()}
                time.sleep(0.2)

            self._last_error = f"Engine did not answer on {self.base_url} within {wait:.0f}s"
            return {"ok": False, "error": self._last_error}

    def stop(self, wait: float = 15.0) -> Dict[str, Any]:
        with self._lock:
            if not self.is_running():
                # No handle — but the port may still be held by an engine this
                # process did not start (adopted, or orphaned by a restart).
                # Returning "already stopped" over a serving port makes Stop a
                # button that does nothing.
                if self._port_pids():
                    self._reap_port()
                    self._note("stopped an engine we did not spawn (reaped the port)")
                    return {"ok": True, "reaped": True}
                return {"ok": True, "already": True}
            proc = self._proc
            assert proc is not None
            try:
                proc.terminate()
            except Exception:
                pass
            deadline = time.time() + wait
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
            else:
                try:
                    proc.kill()
                except Exception:
                    pass
            # the kernel forks helpers; make sure the port is actually free
            self._reap_port()
            self._proc = None
            self._started_at = None
            self._booting_since = None
            self._note("engine stopped")
            return {"ok": True}

    def _reap_port(self) -> None:
        """Best-effort: kill anything still listening on our port."""
        for pid in self._port_pids():
            try:
                if sys.platform == "win32":
                    # `/T` is the whole reason this works: without it taskkill takes the
                    # process that holds the port and leaves the app it spawned, which then
                    # keeps the port (and the VRAM) and answers the next start with "in use".
                    # On POSIX the group kill below is the same idea, and this has to be the
                    # same idea too -- a best-effort reap that reaps half is a leak with a log
                    # line.
                    subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                                   capture_output=True, timeout=15)
                else:
                    # The engine starts a session of its own (below), so the whole
                    # tree — the daemon and the children it forked — goes at once.
                    # Without the session, os.kill would take the parent and leave
                    # an app still holding the port.
                    try:
                        os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError, ValueError):
                        os.kill(int(pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass

    # -- raw engine call -------------------------------------------------

    def call(self, method: str, path: str, body: Any = None,
             query: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method=method)
        with urlopen(req, timeout=timeout) as r:
            raw = r.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {"raw": raw.decode("utf-8", "replace")[:4000]}

# The singleton is built in `plugin_api.py`, which owns the settings callables it takes.
